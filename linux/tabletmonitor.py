#!/usr/bin/env python3
"""Usa um tablet Android ligado por USB como monitor estendido do GNOME no Wayland."""

import argparse
import glob
import os
import shutil
import signal
import socket
import struct
import subprocess
import threading
import time

import gi

gi.require_version('Gst', '1.0')
gi.require_version('GstVideo', '1.0')
gi.require_version('GLibUnix', '2.0')
from gi.repository import Gio, GLib, GLibUnix, Gst, GstVideo

PACKAGE = 'com.allison.tabletmonitor'
MAGIC = b'TMON'
PROTOCOL_VERSION = 1
HELLO = struct.Struct('>4sHHH')  # MAGIC, versão, largura, altura
FRAME = struct.Struct('>IQ')     # tamanho, pts em microssegundos
TOUCH = struct.Struct('>BBff')   # ação, ponteiro, x e y entre 0 e 1
TOUCH_DOWN, TOUCH_MOVE, TOUCH_UP = 0, 1, 2

RD_BUS = 'org.gnome.Mutter.RemoteDesktop'
RD_SESSION = 'org.gnome.Mutter.RemoteDesktop.Session'
SC_BUS = 'org.gnome.Mutter.ScreenCast'
SC_SESSION = 'org.gnome.Mutter.ScreenCast.Session'
SC_STREAM = 'org.gnome.Mutter.ScreenCast.Stream'
DC_BUS = 'org.gnome.Mutter.DisplayConfig'
DC_PATH = '/org/gnome/Mutter/DisplayConfig'
CURSOR_EMBEDDED = 1
CONFIG_TEMPORARY = 1
LAYOUT_LOGICAL = 1
RECONNECT_WAIT_SECONDS = 30
ADB_MISSING_POLLS = 3


def log(message):
    print(time.strftime('%H:%M:%S'), message, flush=True)


def recv_exact(sock, size):
    data = b''
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError('conexão fechada')
        data += chunk
    return data


class Bus:
    def __init__(self):
        self.conn = Gio.bus_get_sync(Gio.BusType.SESSION)

    def call(self, dest, path, iface, method, sig=None, args=None):
        params = GLib.Variant(sig, args) if sig else None
        reply = self.conn.call_sync(dest, path, iface, method, params, None,
                                    Gio.DBusCallFlags.NONE, -1, None)
        return reply.unpack() if reply else None

    def send(self, dest, path, iface, method, sig, args):
        # Sem callback a chamada vai sem esperar resposta e pode sair de qualquer thread.
        self.conn.call(dest, path, iface, method, GLib.Variant(sig, args), None,
                       Gio.DBusCallFlags.NONE, -1, None, None, None)

    def subscribe(self, sender, iface, name, path, handler):
        return self.conn.signal_subscribe(sender, iface, name, path, None,
                                          Gio.DBusSignalFlags.NONE, handler)


def encoder_description(bitrate_kbps, fps):
    """Codificador da placa de vídeo quando existe; senão o do processador.

    O vah264lpenc fica de fora porque nesta Intel ele só aceita qualidade constante, sem bitrate.
    """
    if Gst.ElementFactory.find('vah264enc'):
        return ('vah264enc', 'NV12',
                f'vah264enc bitrate={bitrate_kbps} rate-control=cbr key-int-max={fps * 2} '
                'b-frames=0 target-usage=7')
    threads = min(4, os.cpu_count() or 1)
    return ('openh264enc', 'I420',
            f'openh264enc usage-type=screen complexity=low rate-control=bitrate '
            f'bitrate={bitrate_kbps * 1000} gop-size={fps * 2} multi-thread={threads} '
            'enable-frame-skip=false')


def logical_size(width, height, scale, transform, layout_mode):
    if transform % 2:
        width, height = height, width
    if layout_mode == LAYOUT_LOGICAL:
        return round(width / scale), round(height / scale)
    return width, height


class Session:
    """Um monitor virtual: vídeo para o tablet conectado e toque de volta.

    Quando a conexão cai, o monitor virtual fica aberto por RECONNECT_WAIT_SECONDS esperando o
    tablet voltar, para as janelas não saírem dele a cada queda da USB.
    """

    def __init__(self, bus, width, height, args):
        self.bus = bus
        self.width = width
        self.height = height
        self.args = args
        self.sock = None
        self.saved_layout = []
        self.need_keyframe = True
        self.expire_timer = 0
        self.rd = None
        self.stream = None
        self.node = None
        self.capture_pipeline = None
        self.pipeline = None
        self.restart_timer = 0
        self.virtual_connector = None
        self.cursor_session = None
        self.cursor_pipeline = None
        self.placed = False
        self.closed = False
        self.subscriptions = []
        self.sent_frames = 0
        self.sent_bytes = 0
        self.captured_frames = 0
        self.delay_total = 0
        self.delay_max = 0

    def start(self, sock):
        self.saved_layout = self.bus.call(DC_BUS, DC_PATH, DC_BUS, 'GetCurrentState')[2]
        self.rd = self.bus.call(RD_BUS, '/org/gnome/Mutter/RemoteDesktop', RD_BUS,
                                'CreateSession')[0]
        session_id = self.bus.call(RD_BUS, self.rd, 'org.freedesktop.DBus.Properties', 'Get',
                                   '(ss)', (RD_SESSION, 'SessionId'))[0]
        sc = self.bus.call(SC_BUS, '/org/gnome/Mutter/ScreenCast', SC_BUS, 'CreateSession',
                           '(a{sv})',
                           ({'remote-desktop-session-id': GLib.Variant('s', session_id)},))[0]
        self.stream = self.bus.call(SC_BUS, sc, SC_SESSION, 'RecordVirtual', '(a{sv})',
                                    ({'cursor-mode': GLib.Variant('u', CURSOR_EMBEDDED)},))[0]
        self.subscriptions.append(self.bus.subscribe(
            SC_BUS, SC_STREAM, 'PipeWireStreamAdded', self.stream, self.on_stream_added))
        self.subscriptions.append(self.bus.subscribe(
            RD_BUS, RD_SESSION, 'Closed', self.rd, lambda *_: self.stop()))
        self.subscriptions.append(self.bus.subscribe(
            DC_BUS, DC_BUS, 'MonitorsChanged', DC_PATH, self.on_monitors_changed))
        self.bus.call(RD_BUS, self.rd, RD_SESSION, 'Start')
        if self.args.stats:
            GLib.timeout_add_seconds(2, self.log_stats)
        log(f'tablet conectado, monitor virtual de {self.width}x{self.height}')
        self.attach(sock)

    def attach(self, sock):
        if self.expire_timer:
            GLib.source_remove(self.expire_timer)
            self.expire_timer = 0
        self.sock = sock
        self.need_keyframe = True
        if self.pipeline:
            self.pipeline.get_by_name('sink').send_event(
                GstVideo.video_event_new_upstream_force_key_unit(Gst.CLOCK_TIME_NONE, True, 0))
        threading.Thread(target=self.read_input, args=(sock,), daemon=True).start()

    def detach(self, sock):
        if sock is not self.sock:
            return False
        self.sock = None
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()
        if not self.closed:
            log(f'conexão com o tablet perdida, o monitor virtual fica aberto por '
                f'{RECONNECT_WAIT_SECONDS} s esperando a reconexão')
            self.expire_timer = GLib.timeout_add_seconds(RECONNECT_WAIT_SECONDS, self.expire)
        return False

    def expire(self):
        self.expire_timer = 0
        self.stop()
        return False

    def on_stream_added(self, _conn, _sender, _path, _iface, _signal, params):
        self.node = params.unpack()[0]
        self.start_capture()

    def on_monitors_changed(self, *_):
        # Depois de qualquer mudança na configuração de monitores o GNOME passa a entregar uns
        # 6 quadros/s para o monitor virtual; reconectar a captura volta aos 60 quadros/s.
        if self.restart_timer:
            GLib.source_remove(self.restart_timer)
        self.restart_timer = GLib.timeout_add_seconds(1, self.restart_capture)

    def restart_capture(self):
        self.restart_timer = 0
        if self.pipeline and not self.closed:
            self.stop_pipelines()
            self.need_keyframe = True
            self.start_capture()
        return False

    def stop_pipelines(self):
        for pipeline in (self.capture_pipeline, self.pipeline):
            if pipeline:
                pipeline.set_state(Gst.State.NULL)
                pipeline.get_bus().remove_signal_watch()

    def start_capture(self):
        node = self.node
        fps = self.args.fps
        threads = min(4, os.cpu_count() or 1)
        encoder, pixel_format, encoder_options = encoder_description(self.args.bitrate, fps)
        # O codificador VA só entra no modo de baixo atraso quando a fonte responde que é ao
        # vivo, e o pipewiresrc responde que não é antes de o fluxo começar. Sem o appsrc ao vivo
        # entre a captura e o codificador, cada quadro espera uns 100 ms dentro do codificador.
        capture = (
            f'pipewiresrc name=capture path={node} always-copy=true do-timestamp=true ! '
            f'video/x-raw,width={self.width},height={self.height},max-framerate={fps}/1 ! '
            'queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream ! '
            f'videoconvert n-threads={threads} ! video/x-raw,format={pixel_format} ! '
            'appsink name=frames emit-signals=true sync=false max-buffers=1 drop=true')
        encode = (
            'appsrc name=feed is-live=true format=time '
            'max-buffers=1 max-bytes=0 leaky-type=downstream ! '
            f'{encoder_options} ! '
            'h264parse config-interval=-1 ! video/x-h264,stream-format=byte-stream,alignment=au ! '
            'appsink name=sink emit-signals=true sync=false')
        if not self.pipeline:
            log(f'codificador de vídeo: {encoder}, {fps} quadros/s, {self.args.bitrate} kbit/s')
        self.capture_pipeline = Gst.parse_launch(capture)
        self.pipeline = Gst.parse_launch(encode)
        feed = self.pipeline.get_by_name('feed')
        self.capture_pipeline.get_by_name('frames').connect(
            'new-sample', lambda sink: feed.emit('push-sample', sink.emit('pull-sample')))
        self.pipeline.get_by_name('sink').connect('new-sample', self.on_sample)
        if self.args.stats:
            self.capture_pipeline.get_by_name('capture').get_static_pad('src').add_probe(
                Gst.PadProbeType.BUFFER, self.count_captured)
        for pipeline in (self.capture_pipeline, self.pipeline):
            bus = pipeline.get_bus()
            bus.add_signal_watch()
            bus.connect('message::error', self.on_pipeline_error)
        self.capture_pipeline.set_state(Gst.State.PLAYING)
        # O mesmo relógio e a mesma base de tempo nas duas partes permitem medir o atraso.
        clock = self.capture_pipeline.get_clock()
        if clock:
            self.pipeline.use_clock(clock)
            self.pipeline.set_start_time(Gst.CLOCK_TIME_NONE)
            self.pipeline.set_base_time(self.capture_pipeline.get_base_time())
        self.pipeline.set_state(Gst.State.PLAYING)

    def on_pipeline_error(self, _bus, message):
        error, _debug = message.parse_error()
        log(f'erro no vídeo: {error.message}')
        self.stop()

    def on_sample(self, sink):
        sample = sink.emit('pull-sample')
        buffer = sample.get_buffer()
        if not self.placed:
            self.placed = True
            GLib.idle_add(self.place_monitor)
        sock = self.sock
        if sock is None:
            return Gst.FlowReturn.OK
        # Depois de uma reconexão o decodificador do tablet só começa por um quadro-chave.
        if self.need_keyframe:
            if buffer.has_flags(Gst.BufferFlags.DELTA_UNIT):
                return Gst.FlowReturn.OK
            self.need_keyframe = False
        ok, info = buffer.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        data = bytes(info.data)
        buffer.unmap(info)
        pts = buffer.pts // 1000 if buffer.pts != Gst.CLOCK_TIME_NONE else 0
        try:
            sock.sendall(FRAME.pack(len(data), pts) + data)
        except OSError:
            GLib.idle_add(self.detach, sock)
            return Gst.FlowReturn.OK
        self.sent_frames += 1
        self.sent_bytes += len(data)
        clock = sink.get_clock()
        if self.args.stats and clock and pts:
            captured = sample.get_segment().to_running_time(Gst.Format.TIME, buffer.pts)
            delay = (clock.get_time() - sink.get_base_time() - captured) // 1000
            self.delay_total += delay
            self.delay_max = max(self.delay_max, delay)
        return Gst.FlowReturn.OK

    def count_captured(self, _pad, _info):
        self.captured_frames += 1
        return Gst.PadProbeReturn.OK

    def log_stats(self):
        if self.closed:
            return False
        if self.sent_frames or self.captured_frames:
            delay = self.delay_total / max(1, self.sent_frames) / 1000
            log(f'2 s: {self.captured_frames} quadros capturados, {self.sent_frames} enviados, '
                f'{self.sent_bytes * 8 // 2000} kbit/s, atraso da captura ao envio médio '
                f'{delay:.1f} ms, máximo {self.delay_max / 1000:.1f} ms')
        self.delay_total = 0
        self.delay_max = 0
        self.sent_frames = 0
        self.sent_bytes = 0
        self.captured_frames = 0
        return True

    def place_monitor(self):
        """Mantém a posição dos monitores físicos e põe o tablet ao lado deles."""
        try:
            serial, monitors, logical, props = self.bus.call(
                DC_BUS, DC_PATH, DC_BUS, 'GetCurrentState')
            layout_mode = props.get('layout-mode', LAYOUT_LOGICAL)
            modes = {}
            for (connector, *_), mode_list, _props in monitors:
                for mode in mode_list:
                    if mode[6].get('is-current'):
                        modes[connector] = mode
            present = {connector for (connector, *_), *_ in monitors}
            virtual = next(lm for lm in logical if lm[5][0][0] not in
                           {c[0] for saved in self.saved_layout for c in saved[5]})
            self.virtual_connector = virtual[5][0][0]
            mode_id, mode_w, mode_h, _rate, _pref, scales, _ = modes[self.virtual_connector]
            scale = min(scales, key=lambda s: abs(s - self.args.scale))
            virtual_w, virtual_h = logical_size(mode_w, mode_h, scale, 0, layout_mode)

            config = []
            right = 0
            for x, y, lm_scale, transform, primary, lm_monitors, _ in self.saved_layout:
                if not all(c[0] in present for c in lm_monitors):
                    continue
                first = modes[lm_monitors[0][0]]
                w, _h = logical_size(first[1], first[2], lm_scale, transform, layout_mode)
                right = max(right, x + w)
                config.append([x, y, lm_scale, transform, primary,
                               [(c[0], modes[c[0]][0], {}) for c in lm_monitors]])
            if self.args.position == 'left':
                for lm in config:
                    lm[0] += virtual_w
                config.append([0, 0, scale, 0, False, [(self.virtual_connector, mode_id, {})]])
            else:
                config.append([right, 0, scale, 0, False, [(self.virtual_connector, mode_id, {})]])

            properties = {}
            if props.get('supports-changing-layout-mode'):
                properties['layout-mode'] = GLib.Variant('u', layout_mode)
            self.bus.call(DC_BUS, DC_PATH, DC_BUS, 'ApplyMonitorsConfig',
                          '(uua(iiduba(ssa{sv}))a{sv})',
                          (serial, CONFIG_TEMPORARY, [tuple(lm) for lm in config], properties))
            side = 'esquerda' if self.args.position == 'left' else 'direita'
            log(f'tablet posicionado à {side}, escala {scale:.2f} ({virtual_w}x{virtual_h})')
        except (GLib.Error, StopIteration, KeyError) as error:
            log(f'falhou ao posicionar o monitor do tablet: {error}')
        if self.virtual_connector:
            self.start_cursor_capture()
        return False

    def start_cursor_capture(self):
        """Abre uma captura do tipo monitor, a 1 quadro/s, sobre o monitor virtual.

        Sem ela o GNOME não redesenha o monitor virtual quando só o cursor se move (cerca de
        3 quadros/s); com ela o cursor chega ao tablet a 60 quadros/s.
        """
        if self.closed:
            return False
        try:
            self.cursor_session = self.bus.call(SC_BUS, '/org/gnome/Mutter/ScreenCast', SC_BUS,
                                                'CreateSession', '(a{sv})', ({},))[0]
            stream = self.bus.call(SC_BUS, self.cursor_session, SC_SESSION, 'RecordMonitor',
                                   '(sa{sv})',
                                   (self.virtual_connector,
                                    {'cursor-mode': GLib.Variant('u', CURSOR_EMBEDDED)}))[0]
            self.subscriptions.append(self.bus.subscribe(
                SC_BUS, SC_STREAM, 'PipeWireStreamAdded', stream, self.on_cursor_stream_added))
            self.subscriptions.append(self.bus.subscribe(
                SC_BUS, SC_SESSION, 'Closed', self.cursor_session, self.on_cursor_capture_closed))
            self.bus.call(SC_BUS, self.cursor_session, SC_SESSION, 'Start')
        except GLib.Error as error:
            log(f'falhou ao abrir a captura auxiliar do cursor: {error.message}')
        return False

    def on_cursor_stream_added(self, _conn, _sender, _path, _iface, _signal, params):
        node = params.unpack()[0]
        self.cursor_pipeline = Gst.parse_launch(
            f'pipewiresrc path={node} always-copy=true ! video/x-raw,max-framerate=1/1 ! '
            'fakesink sync=false')
        self.cursor_pipeline.set_state(Gst.State.PLAYING)
        log(f'captura auxiliar do cursor aberta em {self.virtual_connector}')

    def on_cursor_capture_closed(self, *_):
        self.stop_cursor_capture()
        if not self.closed:
            GLib.timeout_add_seconds(1, self.start_cursor_capture)

    def stop_cursor_capture(self):
        if self.cursor_pipeline:
            self.cursor_pipeline.set_state(Gst.State.NULL)
            self.cursor_pipeline = None
        if self.cursor_session:
            try:
                self.bus.call(SC_BUS, self.cursor_session, SC_SESSION, 'Stop')
            except GLib.Error:
                pass
            self.cursor_session = None

    def read_input(self, sock):
        pressed = set()
        try:
            while True:
                action, slot, x, y = TOUCH.unpack(recv_exact(sock, TOUCH.size))
                x = min(max(x, 0.0), 1.0) * (self.width - 1)
                y = min(max(y, 0.0), 1.0) * (self.height - 1)
                if action == TOUCH_DOWN:
                    pressed.add(slot)
                    self.bus.send(RD_BUS, self.rd, RD_SESSION, 'NotifyTouchDown', '(sudd)',
                                  (self.stream, slot, x, y))
                elif action == TOUCH_MOVE:
                    self.bus.send(RD_BUS, self.rd, RD_SESSION, 'NotifyTouchMotion', '(sudd)',
                                  (self.stream, slot, x, y))
                elif action == TOUCH_UP:
                    pressed.discard(slot)
                    self.bus.send(RD_BUS, self.rd, RD_SESSION, 'NotifyTouchUp', '(u)', (slot,))
        except (OSError, ConnectionError):
            pass
        if not self.closed:
            for slot in pressed:
                self.bus.send(RD_BUS, self.rd, RD_SESSION, 'NotifyTouchUp', '(u)', (slot,))
        GLib.idle_add(self.detach, sock)

    def stop(self):
        if self.closed:
            return False
        self.closed = True
        for timer in (self.expire_timer, self.restart_timer):
            if timer:
                GLib.source_remove(timer)
        self.expire_timer = self.restart_timer = 0
        for subscription in self.subscriptions:
            self.bus.conn.signal_unsubscribe(subscription)
        if self.sock:
            self.detach(self.sock)
        self.stop_cursor_capture()
        self.stop_pipelines()
        if self.rd:
            try:
                self.bus.call(RD_BUS, self.rd, RD_SESSION, 'Stop')
            except GLib.Error:
                pass
        log('monitor virtual removido')
        return False


class Server:
    def __init__(self, args):
        self.args = args
        self.bus = Bus()
        self.session = None
        self.listener = socket.create_server(('127.0.0.1', args.port))
        threading.Thread(target=self.accept_loop, daemon=True).start()

    def accept_loop(self):
        while True:
            sock, _ = self.listener.accept()
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            try:
                sock.settimeout(5)
                magic, version, width, height = HELLO.unpack(recv_exact(sock, HELLO.size))
                sock.settimeout(None)
            except (OSError, ConnectionError):
                sock.close()
                continue
            if magic != MAGIC or version != PROTOCOL_VERSION:
                log(f'conexão recusada: versão do aplicativo {version}, '
                    f'esperada {PROTOCOL_VERSION}')
                sock.close()
                continue
            GLib.idle_add(self.open_session, sock, width, height)

    def open_session(self, sock, width, height):
        session = self.session
        if session and not session.closed and (session.width, session.height) == (width, height):
            if session.sock:
                session.detach(session.sock)
            session.attach(sock)
            log('tablet reconectado ao mesmo monitor virtual')
            return False
        if session:
            session.stop()
        self.session = Session(self.bus, width, height, self.args)
        try:
            self.session.start(sock)
        except GLib.Error as error:
            log(f'falhou ao criar o monitor virtual: {error.message}')
            self.session.stop()
        return False


def find_adb():
    adb = shutil.which('adb') or os.path.expanduser('~/Android/Sdk/platform-tools/adb')
    if not os.path.exists(adb):
        raise SystemExit('adb não encontrado')
    return adb


def read_sysfs(path):
    try:
        with open(path) as file:
            return file.read().strip()
    except OSError:
        return ''


def usb_adb_serials():
    """Números de série dos aparelhos na USB com a interface do adb (classe ff, 42, 01)."""
    serials = set()
    for interface in glob.glob('/sys/bus/usb/devices/*:*'):
        kind = [read_sysfs(f'{interface}/{name}') for name in
                ('bInterfaceClass', 'bInterfaceSubClass', 'bInterfaceProtocol')]
        if kind == ['ff', '42', '01']:
            device = os.path.join(os.path.dirname(interface),
                                  os.path.basename(interface).split(':')[0])
            serials.add(read_sysfs(f'{device}/serial'))
    return serials - {''}


def watch_devices(adb, port):
    """Liga a porta do tablet à do PC pelo USB e abre o aplicativo em cada tablet que aparece.

    O servidor adb às vezes deixa de listar o tablet depois de um erro de leitura na USB, mesmo
    com o cabo ligado, e só volta a listá-lo quando é reiniciado.
    """
    ready = set()
    missing = 0
    while True:
        # O transport_id muda a cada reconexão, e cada reconexão apaga o adb reverse no tablet.
        result = subprocess.run([adb, 'devices', '-l'], capture_output=True, text=True)
        transports = set()
        listed = set()
        for line in result.stdout.splitlines()[1:]:
            fields = line.split()
            if fields:
                listed.add(fields[0])
            if len(fields) >= 2 and fields[1] == 'device':
                transport_id = next((f for f in fields if f.startswith('transport_id:')), '')
                transports.add((fields[0], transport_id))
        missing = missing + 1 if usb_adb_serials() - listed else 0
        if missing >= ADB_MISSING_POLLS:
            log('o adb deixou de listar o tablet com a USB ligada, servidor adb reiniciado')
            subprocess.run([adb, 'kill-server'], capture_output=True)
            subprocess.run([adb, 'start-server'], capture_output=True)
            missing = 0
        for transport in transports - ready:
            serial = transport[0]
            reverse = subprocess.run(
                [adb, '-s', serial, 'reverse', f'tcp:{port}', f'tcp:{port}'],
                capture_output=True, text=True)
            if reverse.returncode != 0:
                continue
            launch = subprocess.run([adb, '-s', serial, 'shell', 'am', 'start', '-n',
                                     f'{PACKAGE}/.MainActivity'], capture_output=True, text=True)
            if 'Error' in launch.stdout + launch.stderr:
                log(f'aparelho {serial} sem o aplicativo instalado')
            else:
                log(f'aparelho {serial} ligado por USB, aplicativo aberto')
            ready.add(transport)
        ready &= transports
        time.sleep(2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=27183,
                        help='porta TCP ligada ao tablet pelo adb reverse (padrão: %(default)s)')
    parser.add_argument('--fps', type=int, default=60,
                        help='quadros por segundo no máximo (padrão: %(default)s)')
    parser.add_argument('--bitrate', type=int, default=16000,
                        help='taxa do vídeo em kbit/s (padrão: %(default)s)')
    parser.add_argument('--scale', type=float, default=1.33,
                        help='escala do GNOME no tablet; usa a suportada mais próxima '
                             '(padrão: %(default)s)')
    parser.add_argument('--position', choices=('right', 'left'), default='right',
                        help='lado dos monitores físicos onde o tablet fica '
                             '(padrão: %(default)s)')
    parser.add_argument('--stats', action='store_true',
                        help='registra a cada 2 s os quadros capturados e enviados, a taxa e o '
                             'atraso da captura ao envio')
    args = parser.parse_args()

    Gst.init(None)
    server = Server(args)
    threading.Thread(target=watch_devices, args=(find_adb(), args.port), daemon=True).start()
    log(f'aguardando o tablet na porta {args.port}')

    loop = GLib.MainLoop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signum, loop.quit)
    loop.run()
    if server.session:
        server.session.stop()


if __name__ == '__main__':
    main()
