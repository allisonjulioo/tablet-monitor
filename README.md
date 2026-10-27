# Tablet Monitor

Usa um tablet Android ligado por USB como monitor estendido do GNOME no Wayland. O computador cria um monitor virtual, envia a imagem dele em H.264 pela USB e recebe de volta o toque na tela do tablet.

```
┌──────────────────────┐   H.264 pela USB (adb reverse)   ┌──────────────────────┐
│ computador           │ ───────────────────────────────▶ │ tablet               │
│ linux/tabletmonitor  │                                  │ android/ (app)       │
│                      │ ◀─────────────────────────────── │                      │
└──────────────────────┘          toque na tela           └──────────────────────┘
```

| Pasta | Conteúdo |
| --- | --- |
| [`linux/`](linux/README.md) | Programa do computador: cria o monitor virtual, captura, codifica e repassa o toque ao GNOME. |
| [`android/`](android/README.md) | Aplicativo do tablet: decodifica o vídeo em tela cheia e envia o toque. |

## Requisitos

- GNOME no Wayland. Testado no GNOME 50.1 com Ubuntu.
- Tablet com Android 8.0 ou mais novo e a depuração USB ativada.
- Cabo USB com dados.

## Instalação

1. Compile e instale o aplicativo no tablet ([instruções](android/README.md#compilar-e-instalar)).
2. Instale as dependências e o programa no computador ([instruções](linux/README.md#instalação)):

   ```sh
   ./linux/install.sh
   ```

3. Ligue o tablet na USB e autorize a depuração USB quando o tablet perguntar.

O programa inicia junto com a sessão do GNOME. Quando um tablet com o aplicativo aparece na USB, o programa abre o aplicativo, cria o monitor virtual à direita dos monitores físicos e começa a enviar a imagem.

## Protocolo

Uma conexão TCP em `127.0.0.1:27183` no tablet, levada ao computador pelo `adb reverse`. Todos os números em big-endian.

| Sentido | Mensagem | Formato |
| --- | --- | --- |
| tablet → computador | apresentação, uma vez | `"TMON"`, versão (`u16`, hoje 1), largura e altura da tela em pixels (`u16`, `u16`) |
| computador → tablet | um quadro de vídeo | tamanho (`u32`), pts em microssegundos (`u64`), unidade de acesso H.264 no formato Annex B |
| tablet → computador | um toque | ação (`u8`: 0 encosta, 1 move, 2 solta), ponteiro (`u8`), x e y entre 0 e 1 (`f32`, `f32`) |

O primeiro quadro de cada conexão é um quadro-chave com SPS e PPS.

## Desempenho medido

Intel Iris Xe (codificador `vah264enc`) e moto tab g70 (2000x1200), cursor em movimento contínuo:

- 60 quadros por segundo;
- 10 ms da captura ao envio no computador;
- 1 ms na USB;
- 12 a 19 ms de decodificação no tablet.

## Licença

[MIT](LICENSE)
