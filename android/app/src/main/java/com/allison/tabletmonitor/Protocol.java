package com.allison.tabletmonitor;

import java.nio.ByteBuffer;

/** Mensagens trocadas com o programa do computador, todas em big-endian. */
final class Protocol {

    static final String HOST = "127.0.0.1";
    static final int PORT = 27183;
    static final int VERSION = 1;

    static final byte TOUCH_DOWN = 0;
    static final byte TOUCH_MOVE = 1;
    static final byte TOUCH_UP = 2;

    private Protocol() {
    }

    /** Primeira mensagem do tablet: "TMON", versão, largura e altura da tela em pixels. */
    static byte[] hello(int width, int height) {
        return ByteBuffer.allocate(10)
                .put(new byte[] {'T', 'M', 'O', 'N'})
                .putShort((short) VERSION)
                .putShort((short) width)
                .putShort((short) height)
                .array();
    }

    /** Toque: ação, ponteiro, x e y entre 0 e 1. */
    static byte[] touch(byte action, int pointer, float x, float y) {
        return ByteBuffer.allocate(10)
                .put(action)
                .put((byte) pointer)
                .putFloat(x)
                .putFloat(y)
                .array();
    }
}
