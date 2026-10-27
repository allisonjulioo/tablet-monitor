package com.allison.tabletmonitor;

import android.media.MediaCodec;
import android.media.MediaFormat;
import android.os.Build;
import android.util.Log;
import android.view.Surface;

import java.io.IOException;
import java.io.InterruptedIOException;
import java.nio.ByteBuffer;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Decodifica o H.264 do computador direto na Surface da tela.
 *
 * <p>Com {@code adb shell setprop log.tag.TabletMonitor DEBUG} registra a cada 2 s os quadros
 * mostrados e o tempo entre a entrada de cada quadro no decodificador e a saída dele.
 */
final class VideoDecoder {

    private static final String TAG = "TabletMonitor";
    private static final long INPUT_TIMEOUT_US = 10_000;
    private static final long OUTPUT_TIMEOUT_US = 100_000;
    private static final long STATS_INTERVAL_NS = 2_000_000_000L;

    private final MediaCodec codec;
    private final Thread drainThread;
    private final boolean logStats = Log.isLoggable(TAG, Log.DEBUG);
    private final ConcurrentHashMap<Long, Long> queuedAt = new ConcurrentHashMap<>();

    /** O primeiro quadro precisa ser um quadro-chave, com o SPS e o PPS. */
    VideoDecoder(Surface surface, int width, int height, byte[] firstFrame, int length)
            throws IOException {
        MediaFormat format =
                MediaFormat.createVideoFormat(MediaFormat.MIMETYPE_VIDEO_AVC, width, height);
        ByteBuffer[] parameterSets = parameterSets(firstFrame, length);
        if (parameterSets != null) {
            format.setByteBuffer("csd-0", parameterSets[0]);
            format.setByteBuffer("csd-1", parameterSets[1]);
        }
        if (Build.VERSION.SDK_INT >= 30) {
            format.setInteger(MediaFormat.KEY_LOW_LATENCY, 1);
        }
        format.setInteger(MediaFormat.KEY_PRIORITY, 0);
        codec = MediaCodec.createDecoderByType(MediaFormat.MIMETYPE_VIDEO_AVC);
        codec.configure(format, surface, null, 0);
        codec.start();
        drainThread = new Thread(this::drain, "decoder-output");
        drainThread.start();
    }

    /** Espera um buffer de entrada livre; para se a thread for interrompida enquanto espera. */
    void queue(byte[] frame, int length, long pts) throws InterruptedIOException {
        int index;
        while ((index = codec.dequeueInputBuffer(INPUT_TIMEOUT_US)) < 0) {
            if (Thread.currentThread().isInterrupted()) {
                throw new InterruptedIOException();
            }
        }
        ByteBuffer buffer = codec.getInputBuffer(index);
        buffer.clear();
        buffer.put(frame, 0, length);
        if (logStats) {
            queuedAt.put(pts, System.nanoTime());
        }
        codec.queueInputBuffer(index, 0, length, pts, 0);
    }

    void release() {
        drainThread.interrupt();
        try {
            drainThread.join(1000);
        } catch (InterruptedException ignored) {
            Thread.currentThread().interrupt();
        }
        codec.stop();
        codec.release();
    }

    private void drain() {
        MediaCodec.BufferInfo info = new MediaCodec.BufferInfo();
        int frames = 0;
        long delaySum = 0;
        long delayMax = 0;
        long lastLog = System.nanoTime();
        try {
            while (!Thread.currentThread().isInterrupted()) {
                int index = codec.dequeueOutputBuffer(info, OUTPUT_TIMEOUT_US);
                if (index >= 0) {
                    codec.releaseOutputBuffer(index, true);
                    Long queued = queuedAt.remove(info.presentationTimeUs);
                    if (queued != null) {
                        long delay = System.nanoTime() - queued;
                        frames++;
                        delaySum += delay;
                        delayMax = Math.max(delayMax, delay);
                    }
                }
                long now = System.nanoTime();
                if (logStats && now - lastLog >= STATS_INTERVAL_NS) {
                    if (frames > 0) {
                        Log.d(TAG, String.format(
                                "%s: %d quadros em 2 s, decodificação média %.1f ms, máxima %.1f ms",
                                codec.getName(), frames, delaySum / 1e6 / frames, delayMax / 1e6));
                    }
                    frames = 0;
                    delaySum = 0;
                    delayMax = 0;
                    lastLog = now;
                }
            }
        } catch (IllegalStateException ignored) {
            // O decodificador foi parado enquanto esperava saída.
        }
        queuedAt.clear();
    }

    /** Separa o SPS e o PPS de um quadro no formato Annex B. */
    private static ByteBuffer[] parameterSets(byte[] data, int length) {
        ByteBuffer sps = null;
        ByteBuffer pps = null;
        int start = -1;
        int i = 0;
        while (i <= length) {
            boolean startCode = i + 2 < length
                    && data[i] == 0 && data[i + 1] == 0 && data[i + 2] == 1;
            if (startCode || i == length) {
                if (start >= 0) {
                    int end = i;
                    while (end > start && data[end - 1] == 0) {
                        end--;
                    }
                    int type = data[start] & 0x1f;
                    if (type == 7 || type == 8) {
                        ByteBuffer nal = ByteBuffer.allocate(4 + end - start);
                        nal.put(new byte[] {0, 0, 0, 1}).put(data, start, end - start).flip();
                        if (type == 7) {
                            sps = nal;
                        } else {
                            pps = nal;
                        }
                    }
                }
                if (i == length) {
                    break;
                }
                i += 3;
                start = i;
            } else {
                i++;
            }
        }
        return sps != null && pps != null ? new ByteBuffer[] {sps, pps} : null;
    }
}
