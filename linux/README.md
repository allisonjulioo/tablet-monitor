# Programa do computador

`tabletmonitor.py` cria um monitor virtual no GNOME do tamanho da tela do tablet, envia a imagem dele em H.264 e repassa o toque do tablet ao GNOME. Usa as APIs D-Bus do Mutter (`RemoteDesktop`, `ScreenCast` e `DisplayConfig`), o PipeWire para a captura e o GStreamer para a codificação.

## Instalação

Dependências no Ubuntu:

```sh
sudo apt install python3-gi gir1.2-glib-2.0 gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
    gstreamer1.0-pipewire gstreamer1.0-plugins-base gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-extra adb
```

Para codificar na placa de vídeo Intel, instale também `intel-media-va-driver-non-free`. Sem codificador VA, o programa usa o `openh264enc` no processador, que chegou a 37 quadros/s em 2000x1200 na máquina de teste.

O `adb` pode vir do pacote `adb` ou do Android SDK em `~/Android/Sdk/platform-tools`.

Instalar e iniciar com a sessão do GNOME:

```sh
./install.sh
```

O script copia o programa para `~/.local/bin/tabletmonitor` e o serviço para `~/.config/systemd/user/tabletmonitor.service`. Para remover: `./uninstall.sh`.

Registro do serviço:

```sh
journalctl --user -u tabletmonitor -f
```

## Execução manual

Pare o serviço antes, porque os dois usam a mesma porta:

```sh
systemctl --user stop tabletmonitor
./tabletmonitor.py --stats
```

| Opção | Padrão | Efeito |
| --- | --- | --- |
| `--port` | `27183` | porta TCP ligada ao tablet pelo `adb reverse` |
| `--fps` | `60` | quadros por segundo no máximo |
| `--bitrate` | `16000` | taxa do vídeo em kbit/s |
| `--scale` | `1.33` | escala do GNOME no tablet; usa a suportada mais próxima |
| `--position` | `right` | lado dos monitores físicos onde o tablet fica (`right` ou `left`) |
| `--stats` | | registra a cada 2 s os quadros capturados e enviados, a taxa e o atraso da captura ao envio |

Para mudar as opções do serviço, edite a linha `ExecStart` de `tabletmonitor.service` e rode `./install.sh` de novo.

## Funcionamento

1. A cada 2 s o programa lista os aparelhos do `adb`. Para cada aparelho novo, liga a porta 27183 do tablet à do computador (`adb reverse`) e abre o aplicativo.
2. Quando o aplicativo conecta e envia o tamanho da tela, o programa cria uma sessão `RemoteDesktop` com um `ScreenCast.RecordVirtual`, que é o monitor virtual.
3. No primeiro quadro, posiciona o monitor virtual ao lado dos monitores físicos com `ApplyMonitorsConfig` temporário, sem mudar a posição dos físicos.
4. Captura o monitor virtual pelo PipeWire, converte para NV12 e codifica em H.264 sem quadros B.
5. Cada toque recebido vira `NotifyTouchDown`, `NotifyTouchMotion` ou `NotifyTouchUp` no monitor virtual.
6. Se a conexão cai, o monitor virtual fica aberto por 30 s. Se o tablet volta nesse tempo com a mesma resolução, recebe o mesmo monitor, e as janelas continuam nele.

## Comportamentos do GNOME e do GStreamer que o programa contorna

- **Cursor a 3 quadros/s.** Quando só o cursor se move, o GNOME redesenha o monitor virtual a uns 3 quadros/s. O programa abre uma segunda captura, do tipo `RecordMonitor`, sobre o monitor virtual, a 1 quadro/s; com ela o cursor chega ao tablet a 60 quadros/s.
- **6 quadros/s depois de mudar os monitores.** Depois de qualquer mudança na configuração de monitores, a captura do monitor virtual cai para uns 6 quadros/s. O programa reconecta a captura 1 s depois de cada sinal `MonitorsChanged`.
- **100 ms de atraso no codificador.** O `vah264enc` só usa o modo de baixo atraso quando a fonte informa que é ao vivo, e o `pipewiresrc` informa que não é até o fluxo começar. A captura e a codificação ficam em dois pipelines ligados por um `appsrc is-live=true`.
- **Servidor adb sem o tablet.** Depois de um erro de leitura na USB, o servidor adb às vezes deixa de listar o tablet com o cabo ligado. Quando o kernel mostra uma interface adb na USB que o `adb devices` não lista por 6 s, o programa reinicia o servidor adb.
- **`vah264lpenc` fora.** Na Intel Iris Xe testada ele só aceita qualidade constante (`cqp`), sem controle de taxa.
