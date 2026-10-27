# Aplicativo do tablet

Aplicativo Android em Java, sem bibliotecas externas. Mostra em tela cheia o vídeo que o computador envia e devolve o toque.

## Compilar e instalar

Precisa do JDK 17 ou mais novo e do Android SDK com a plataforma 36. Informe o SDK em `local.properties` (`sdk.dir=/caminho/do/Android/Sdk`) ou na variável `ANDROID_HOME`.

```sh
./gradlew assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

No tablet, ative as opções do desenvolvedor e a depuração USB. Na primeira ligação, autorize o computador quando o tablet perguntar.

## Funcionamento

1. Ao abrir, o aplicativo tenta conectar em `127.0.0.1:27183` a cada segundo. O `adb reverse` feito pelo programa do computador leva essa porta ao computador pela USB.
2. Envia a apresentação com a resolução física da tela em paisagem, que vira o tamanho do monitor virtual.
3. Cria o decodificador H.264 do aparelho (`MediaCodec`) com o SPS e o PPS do primeiro quadro, com baixo atraso ativado, e mostra cada quadro direto na `SurfaceView`.
4. Envia cada toque com as coordenadas divididas pela largura e pela altura da tela.
5. Quando a conexão cai, mostra "Aguardando o computador" e volta a tentar.

| Arquivo | Conteúdo |
| --- | --- |
| `MainActivity.java` | tela cheia, conexão, recebimento dos quadros e envio do toque |
| `VideoDecoder.java` | decodificador H.264 e estatística de decodificação |
| `Protocol.java` | formato das mensagens trocadas com o computador |

## Diagnóstico

Para registrar a cada 2 s os quadros mostrados e o tempo de decodificação:

```sh
adb shell setprop log.tag.TabletMonitor DEBUG
adb logcat -s TabletMonitor
```

A propriedade vale até o tablet reiniciar. Ela é lida quando o decodificador é criado, então reabra o aplicativo depois de mudá-la.
