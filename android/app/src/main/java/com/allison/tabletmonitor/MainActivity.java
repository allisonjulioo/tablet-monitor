package com.allison.tabletmonitor;

import android.app.Activity;
import android.graphics.Color;
import android.os.Build;
import android.os.Bundle;
import android.view.Display;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.Surface;
import android.view.SurfaceHolder;
import android.view.SurfaceView;
import android.view.View;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.view.WindowManager;
import android.widget.FrameLayout;
import android.widget.TextView;

import java.io.BufferedInputStream;
import java.io.DataInputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** Mostra em tela cheia o monitor virtual que o computador envia pela USB e devolve o toque. */
public class MainActivity extends Activity implements SurfaceHolder.Callback {

    private static final int CONNECT_TIMEOUT_MS = 1000;
    private static final int RETRY_INTERVAL_MS = 1000;
    private static final int READ_BUFFER_BYTES = 1 << 20;

    private final ExecutorService touchSender = Executors.newSingleThreadExecutor();
    private TextView status;
    private volatile Surface surface;
    private volatile boolean running;
    private volatile Socket socket;
    private volatile OutputStream output;
    private Thread streamThread;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        if (Build.VERSION.SDK_INT >= 28) {
            getWindow().getAttributes().layoutInDisplayCutoutMode =
                    WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES;
        }

        SurfaceView surfaceView = new SurfaceView(this);
        surfaceView.getHolder().addCallback(this);
        surfaceView.setOnTouchListener(this::onTouch);

        status = new TextView(this);
        status.setText(R.string.waiting_for_computer);
        status.setTextColor(Color.GRAY);
        status.setTextSize(20);
        status.setGravity(Gravity.CENTER);

        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.BLACK);
        root.addView(surfaceView);
        root.addView(status);
        setContentView(root);
        hideSystemBars();
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) {
            hideSystemBars();
        }
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        touchSender.shutdownNow();
    }

    @Override
    public void surfaceCreated(SurfaceHolder holder) {
        surface = holder.getSurface();
        running = true;
        streamThread = new Thread(this::streamLoop, "stream");
        streamThread.start();
    }

    @Override
    public void surfaceChanged(SurfaceHolder holder, int format, int width, int height) {
    }

    @Override
    public void surfaceDestroyed(SurfaceHolder holder) {
        running = false;
        closeSocket();
        streamThread.interrupt();
        try {
            streamThread.join(2000);
        } catch (InterruptedException ignored) {
            Thread.currentThread().interrupt();
        }
        surface = null;
    }

    /** Conecta ao computador e reconecta a cada segundo enquanto a tela existir. */
    private void streamLoop() {
        while (running) {
            try (Socket s = new Socket()) {
                socket = s;
                s.connect(new InetSocketAddress(Protocol.HOST, Protocol.PORT), CONNECT_TIMEOUT_MS);
                s.setTcpNoDelay(true);
                receiveVideo(s);
            } catch (IOException | RuntimeException ignored) {
                // Computador ausente ou conexão perdida: tenta de novo.
            } finally {
                socket = null;
                output = null;
                setStatusVisible(true);
            }
            if (running) {
                try {
                    Thread.sleep(RETRY_INTERVAL_MS);
                } catch (InterruptedException e) {
                    return;
                }
            }
        }
    }

    /** Envia o tamanho da tela e mostra cada quadro recebido até a conexão cair. */
    private void receiveVideo(Socket s) throws IOException {
        int[] size = displaySize();
        OutputStream out = s.getOutputStream();
        out.write(Protocol.hello(size[0], size[1]));
        out.flush();
        output = out;

        DataInputStream in =
                new DataInputStream(new BufferedInputStream(s.getInputStream(), READ_BUFFER_BYTES));
        byte[] frame = new byte[READ_BUFFER_BYTES];
        VideoDecoder decoder = null;
        try {
            while (running) {
                int length = in.readInt();
                long pts = in.readLong();
                if (length > frame.length) {
                    frame = new byte[length];
                }
                in.readFully(frame, 0, length);
                if (decoder == null) {
                    decoder = new VideoDecoder(surface, size[0], size[1], frame, length);
                    setStatusVisible(false);
                }
                decoder.queue(frame, length, pts);
            }
        } finally {
            if (decoder != null) {
                decoder.release();
            }
        }
    }

    /** Tamanho físico da tela em paisagem, que é o tamanho do monitor virtual. */
    @SuppressWarnings("deprecation")
    private int[] displaySize() {
        Display.Mode mode = getWindowManager().getDefaultDisplay().getMode();
        int a = mode.getPhysicalWidth();
        int b = mode.getPhysicalHeight();
        return new int[] {Math.max(a, b), Math.min(a, b)};
    }

    private boolean onTouch(View view, MotionEvent event) {
        switch (event.getActionMasked()) {
            case MotionEvent.ACTION_DOWN:
            case MotionEvent.ACTION_POINTER_DOWN:
                sendTouch(Protocol.TOUCH_DOWN, view, event, event.getActionIndex());
                break;
            case MotionEvent.ACTION_MOVE:
                for (int i = 0; i < event.getPointerCount(); i++) {
                    sendTouch(Protocol.TOUCH_MOVE, view, event, i);
                }
                break;
            case MotionEvent.ACTION_UP:
            case MotionEvent.ACTION_POINTER_UP:
                sendTouch(Protocol.TOUCH_UP, view, event, event.getActionIndex());
                break;
            case MotionEvent.ACTION_CANCEL:
                for (int i = 0; i < event.getPointerCount(); i++) {
                    sendTouch(Protocol.TOUCH_UP, view, event, i);
                }
                break;
            default:
                break;
        }
        return true;
    }

    private void sendTouch(byte action, View view, MotionEvent event, int index) {
        OutputStream out = output;
        if (out == null) {
            return;
        }
        byte[] message = Protocol.touch(action, event.getPointerId(index),
                event.getX(index) / view.getWidth(), event.getY(index) / view.getHeight());
        touchSender.execute(() -> {
            try {
                out.write(message);
            } catch (IOException ignored) {
                // A conexão caiu; o streamLoop reconecta.
            }
        });
    }

    @SuppressWarnings("deprecation")
    private void hideSystemBars() {
        if (Build.VERSION.SDK_INT >= 30) {
            getWindow().setDecorFitsSystemWindows(false);
            WindowInsetsController controller = getWindow().getInsetsController();
            if (controller != null) {
                controller.hide(WindowInsets.Type.systemBars());
                controller.setSystemBarsBehavior(
                        WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
            }
        } else {
            getWindow().getDecorView().setSystemUiVisibility(
                    View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                            | View.SYSTEM_UI_FLAG_FULLSCREEN
                            | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                            | View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                            | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                            | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION);
        }
    }

    private void setStatusVisible(boolean visible) {
        runOnUiThread(() -> status.setVisibility(visible ? View.VISIBLE : View.GONE));
    }

    private void closeSocket() {
        Socket s = socket;
        if (s != null) {
            try {
                s.close();
            } catch (IOException ignored) {
                // Já fechado.
            }
        }
    }
}
