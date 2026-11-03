import http.server
import io
import json
import queue
import socket
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

# -------- Configuracion --------
PUERTO = 8080
CARPETA_IMG = Path("img")
ABRIR_NAVEGADOR = True
EXTENSIONES = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.avif', '.svg'}
TIPOS_MIME = {
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
    '.png': 'image/png', '.gif': 'image/gif',
    '.webp': 'image/webp', '.bmp': 'image/bmp',
    '.avif': 'image/avif', '.svg': 'image/svg+xml',
}

try:
    from PIL import Image as PILImage
    HAY_PIL = True
except ImportError:
    HAY_PIL = False


# -------- Estado compartido --------
_lock = threading.Lock()
_sus_lock = threading.Lock()
imagenes = []
indice = 0
suscriptores = []


def listar_imagenes():
    if not CARPETA_IMG.is_dir():
        return []
    return sorted(
        f.name for f in CARPETA_IMG.iterdir()
        if f.is_file() and f.suffix.lower() in EXTENSIONES
    )


def actualizar_lista():
    global imagenes, indice
    nueva = listar_imagenes()
    with _lock:
        if nueva != imagenes:
            imagenes = nueva
            if indice >= len(imagenes):
                indice = 0
        return imagenes


def obtener_estado():
    with _lock:
        total = len(imagenes)
        return {
            'index': indice,
            'total': total,
            'imagen': imagenes[indice] if total else None,
        }


def notificar():
    est = obtener_estado()
    with _sus_lock:
        for q in list(suscriptores):
            try:
                q.put_nowait(est)
            except queue.Full:
                pass


def fijar_indice(nuevo):
    global indice
    with _lock:
        total = len(imagenes)
        if total:
            if nuevo < 0:
                nuevo = total - 1
            elif nuevo >= total:
                nuevo = 0
            indice = nuevo
    notificar()
    return obtener_estado()


# -------- HTML: visor (PC / OBS) --------
PAGINA_VISOR = r'''<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<title>Visor</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  html, body { width:100%; height:100%; background:#000; overflow:hidden; }
  #stage { position:fixed; inset:0; }
  .capa {
    position:absolute; inset:0; width:100%; height:100%;
    object-fit:contain; opacity:0; transition:opacity .35s ease;
    will-change:opacity;
  }
  .capa.activa { opacity:1; }
  #info {
    position:fixed; top:10px; right:14px;
    background:rgba(0,0,0,.55); color:#fff; padding:4px 10px;
    border-radius:999px; font-family:system-ui,sans-serif; font-size:13px;
    pointer-events:none; font-variant-numeric:tabular-nums;
  }
</style>
</head>
<body>
<div id="stage">
  <img id="capaA" class="capa" alt="">
  <img id="capaB" class="capa" alt="">
</div>
<div id="info"><span id="i">0</span> / <span id="t">0</span></div>
<script>
(function () {
  var capas = [document.getElementById('capaA'), document.getElementById('capaB')];
  var activa = 0;
  var actual = null;
  var estado = { index: 0, total: 0, imagen: null };

  function mostrar(nombre) {
    if (!nombre || nombre === actual) return;
    actual = nombre;
    var inact = 1 - activa;
    var img = capas[inact];
    var hecho = false;
    function alCargar() {
      if (hecho) return;
      hecho = true;
      img.classList.add('activa');
      capas[activa].classList.remove('activa');
      activa = inact;
      img.removeEventListener('load', alCargar);
    }
    img.addEventListener('load', alCargar);
    img.src = '/img/' + encodeURIComponent(nombre);
    if (img.complete) setTimeout(alCargar, 0);
  }

  function aplicar(est) {
    estado = est;
    document.getElementById('i').textContent = est.total ? est.index + 1 : 0;
    document.getElementById('t').textContent = est.total;
    mostrar(est.imagen);
  }

  var ev = new EventSource('/api/events');
  ev.onmessage = function (e) {
    try { aplicar(JSON.parse(e.data)); } catch (_) {}
  };

  function enviar(i) {
    fetch('/api/state', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({index:i})
    }).catch(function(){});
  }

  document.addEventListener('keydown', function (e) {
    if (!estado.total) return;
    if (e.key === 'ArrowRight' || e.key === 'd' || e.key === 'D' || e.key === 'PageDown') {
      e.preventDefault(); enviar(estado.index + 1);
    } else if (e.key === 'ArrowLeft' || e.key === 'a' || e.key === 'A' || e.key === 'PageUp') {
      e.preventDefault(); enviar(estado.index - 1);
    } else if (e.key === 'Home') { e.preventDefault(); enviar(0); }
    else if (e.key === 'End')  { e.preventDefault(); enviar(estado.total - 1); }
  });
})();
</script>
</body>
</html>
'''


# -------- HTML: control (tablet) --------
PAGINA_CONTROL = r'''<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>Control</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
  html, body {
    height:100%; background:#0b0b0d; color:#fff; overflow:hidden;
    font-family:system-ui,-apple-system,sans-serif;
    user-select:none; -webkit-user-select:none;
  }
  #top {
    padding:10px 16px; background:#000; height:48px;
    display:flex; justify-content:space-between; align-items:center;
    border-bottom:1px solid #232323;
  }
  #top h1 { font-size:15px; font-weight:500; letter-spacing:.3px; }
  #top .num { font-size:13px; color:#9aa; font-variant-numeric:tabular-nums; }
  #grid {
    position:absolute; top:48px; bottom:88px; left:0; right:0;
    overflow-y:auto; padding:8px;
    display:grid; gap:8px;
    grid-template-columns:repeat(auto-fill, minmax(130px, 1fr));
    align-content:start;
    -webkit-overflow-scrolling:touch;
  }
  .cel {
    position:relative; aspect-ratio:4 / 3; overflow:hidden;
    border-radius:8px; background:#16161a; cursor:pointer;
    border:2px solid transparent; opacity:.65;
    transition:opacity .15s, border-color .15s, transform .1s;
  }
  .cel img { width:100%; height:100%; object-fit:cover; display:block; pointer-events:none; }
  .cel.activa {
    border-color:#3b82f6; opacity:1;
    box-shadow:0 0 0 2px rgba(59,130,246,.35);
  }
  .cel:active { transform:scale(.97); }
  #barra {
    position:fixed; bottom:0; left:0; right:0; height:88px;
    background:#000; border-top:1px solid #232323;
    display:flex; gap:8px; padding:8px;
  }
  #barra button {
    flex:1; background:#1b1e26; color:#fff; border:none;
    border-radius:12px; font-size:24px; cursor:pointer;
    transition:background .1s;
  }
  #barra button:active { background:#2d3342; }
  #barra button:disabled { opacity:.3; }
  #sinimg {
    position:absolute; inset:48px 0 88px 0;
    display:none; align-items:center; justify-content:center;
    color:#778; padding:20px; text-align:center; font-size:14px;
  }
</style>
</head>
<body>
<div id="top">
  <h1>Control</h1>
  <span class="num"><span id="i">0</span> / <span id="t">0</span></span>
</div>
<div id="grid"></div>
<div id="sinimg">No hay imágenes en la carpeta <b>img/</b>.</div>
<div id="barra">
  <button id="b-prev" aria-label="Anterior">◀</button>
  <button id="b-first" aria-label="Primera">⏮</button>
  <button id="b-last" aria-label="Última">⏭</button>
  <button id="b-next" aria-label="Siguiente">▶</button>
</div>
<script>
(function () {
  var estado = { index: 0, total: 0, imagen: null };
  var celdas = [];
  var grid = document.getElementById('grid');
  var sinimg = document.getElementById('sinimg');

  function cargarMiniaturas() {
    fetch('/api/images').then(function (r) { return r.json(); }).then(function (lista) {
      grid.innerHTML = ''; celdas = [];
      if (!lista.length) { sinimg.style.display = 'flex'; return; }
      sinimg.style.display = 'none';
      var frag = document.createDocumentFragment();
      lista.forEach(function (nombre, i) {
        var cel = document.createElement('div');
        cel.className = 'cel'; cel.dataset.i = i;
        var img = document.createElement('img');
        img.src = '/api/thumb/' + encodeURIComponent(nombre);
        img.loading = 'lazy'; img.alt = nombre;
        cel.appendChild(img);
        cel.addEventListener('click', function () { irA(i); });
        frag.appendChild(cel);
        celdas.push(cel);
      });
      grid.appendChild(frag);
      marcar();
    }).catch(function () {});
  }

  function irA(i) {
    fetch('/api/state', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({index:i})
    }).catch(function(){});
  }

  function marcar() {
    celdas.forEach(function (c, i) {
      c.classList.toggle('activa', i === estado.index);
    });
    var c = celdas[estado.index];
    if (c && c.scrollIntoView) c.scrollIntoView({ block:'nearest', behavior:'smooth' });
    document.getElementById('i').textContent = estado.total ? estado.index + 1 : 0;
    document.getElementById('t').textContent = estado.total;
    ['b-prev','b-next','b-first','b-last'].forEach(function (id) {
      document.getElementById(id).disabled = !estado.total;
    });
  }

  var ev = new EventSource('/api/events');
  ev.onmessage = function (e) {
    try { estado = JSON.parse(e.data); marcar(); } catch (_) {}
  };

  document.getElementById('b-prev').addEventListener('click', function () { irA(estado.index - 1); });
  document.getElementById('b-next').addEventListener('click', function () { irA(estado.index + 1); });
  document.getElementById('b-first').addEventListener('click', function () { irA(0); });
  document.getElementById('b-last').addEventListener('click', function () { irA(estado.total - 1); });

  document.addEventListener('keydown', function (e) {
    if (!estado.total) return;
    if (e.key === 'ArrowRight') irA(estado.index + 1);
    else if (e.key === 'ArrowLeft') irA(estado.index - 1);
  });

  cargarMiniaturas();
})();
</script>
</body>
</html>
'''


# -------- Handler HTTP --------
class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "Visor/1.0"

    def log_message(self, fmt, *args):
        pass

    def _enviar(self, data, ct='text/html; charset=utf-8', status=200, cache='no-store'):
        if isinstance(data, str):
            data = data.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', ct)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', cache)
        self.end_headers()
        self.wfile.write(data)

    def _enviar_json(self, obj, status=200):
        self._enviar(json.dumps(obj).encode('utf-8'),
                     'application/json; charset=utf-8', status)

    def _resolver(self, nombre):
        nombre = urllib.parse.unquote(nombre)
        archivo = (CARPETA_IMG / nombre).resolve()
        try:
            archivo.relative_to(CARPETA_IMG.resolve())
        except ValueError:
            return None
        return archivo if archivo.is_file() else None

    def _servir_imagen(self, nombre):
        archivo = self._resolver(nombre)
        if archivo is None:
            self.send_error(404); return
        ct = TIPOS_MIME.get(archivo.suffix.lower(), 'application/octet-stream')
        self._enviar(archivo.read_bytes(), ct, cache='max-age=3600')

    def _servir_thumb(self, nombre):
        archivo = self._resolver(nombre)
        if archivo is None:
            self.send_error(404); return
        if HAY_PIL:
            try:
                im = PILImage.open(archivo)
                im.thumbnail((320, 320))
                buf = io.BytesIO()
                im.convert('RGB').save(buf, format='JPEG', quality=80)
                self._enviar(buf.getvalue(), 'image/jpeg', cache='max-age=3600')
                return
            except Exception:
                pass
        ct = TIPOS_MIME.get(archivo.suffix.lower(), 'application/octet-stream')
        self._enviar(archivo.read_bytes(), ct, cache='max-age=3600')

    def _sse(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('X-Accel-Buffering', 'no')
        self.end_headers()

        q = queue.Queue(maxsize=50)
        with _sus_lock:
            suscriptores.append(q)
        try:
            self.wfile.write(
                ('data: ' + json.dumps(obtener_estado()) + '\n\n').encode())
            self.wfile.flush()
            while True:
                try:
                    est = q.get(timeout=20)
                    self.wfile.write(
                        ('data: ' + json.dumps(est) + '\n\n').encode())
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b': ping\n\n')
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with _sus_lock:
                try: suscriptores.remove(q)
                except ValueError: pass

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path in ('/', '/index.html'):
                self._enviar(PAGINA_VISOR)
            elif path in ('/control', '/control.html'):
                self._enviar(PAGINA_CONTROL)
            elif path == '/api/state':
                self._enviar_json(obtener_estado())
            elif path == '/api/images':
                actualizar_lista()
                self._enviar_json(listar_imagenes())
            elif path == '/api/events':
                self._sse()
            elif path.startswith('/api/thumb/'):
                self._servir_thumb(path[len('/api/thumb/'):])
            elif path.startswith('/img/'):
                self._servir_imagen(path[len('/img/'):])
            else:
                self.send_error(404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == '/api/state':
            try:
                n = int(self.headers.get('Content-Length', '0'))
                body = self.rfile.read(n) if n else b'{}'
                data = json.loads(body or b'{}')
                nuevo = int(data.get('index', 0))
                self._enviar_json(fijar_indice(nuevo))
            except Exception as e:
                self._enviar_json({'error': str(e)}, 400)
        elif path == '/api/refresh':
            actualizar_lista()
            self._enviar_json({'imagenes': listar_imagenes()})
        else:
            self.send_error(404)


# -------- Arranque --------
def obtener_ip_local():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


def _abrir_navegador():
    time.sleep(1.0)
    try:
        webbrowser.open(f'http://localhost:{PUERTO}/')
    except Exception:
        pass


def main():
    if not CARPETA_IMG.is_dir():
        CARPETA_IMG.mkdir(parents=True, exist_ok=True)
        print(f'Se creó la carpeta "{CARPETA_IMG}". Coloca tus imágenes dentro y vuelve a ejecutar.')
        return

    img_list = actualizar_lista()
    if not img_list:
        print(f'No hay imágenes en "{CARPETA_IMG}". Añade algunas y vuelve a ejecutar.')
        return

    ip = obtener_ip_local()
    print('=' * 60)
    print("ViewerImg Stream versión 1.0")
    print("Desarrollado por Totir Deloper - MIT License")
    print ("Link del código fuente: https://github.com/TotirDeloper/Viewer-Img-Steam")
    print('=' * 60)
    print("Repositorio del código ")
    print(f'Imágenes encontradas: {len(img_list)}')
    print(f'Pillow disponible: {"sí" if HAY_PIL else "no (miniaturas a tamaño original)"}')
    print()
    print(f'  Visor (PC / OBS):   http://localhost:{PUERTO}/')
    print(f'  Control (tablet):   http://{ip}:{PUERTO}/control')
    print()
    print('  OBS -> Fuente navegador -> URL de arriba.')
    print('  (Mismo Wi-Fi / misma red entre tablet y PC)')
    print('=' * 60)
    print('Ctrl+C para detener.\n')

    if ABRIR_NAVEGADOR:
        threading.Thread(target=_abrir_navegador, daemon=True).start()

    servidor = http.server.ThreadingHTTPServer(('0.0.0.0', PUERTO), Handler)
    servidor.daemon_threads = True
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print('\nCerrando...')
    finally:
        servidor.shutdown()


if __name__ == '__main__':
    main()
