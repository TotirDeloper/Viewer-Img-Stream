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
CARPETA_IMG = Path("files")
ABRIR_NAVEGADOR = True
EXTENSIONES_IMG = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.avif', '.svg'}
EXTENSIONES_PDF = {'.pdf'}
TIPOS_MIME = {
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
    '.png': 'image/png', '.gif': 'image/gif',
    '.webp': 'image/webp', '.bmp': 'image/bmp',
    '.avif': 'image/avif', '.svg': 'image/svg+xml',
}
PDF_DPI_COMPLETA = 150   # resolucion del slide a pantalla completa
PDF_DPI_MINIATURA = 40   # resolucion de miniaturas (control)

try:
    from PIL import Image as PILImage
    HAY_PIL = True
except ImportError:
    HAY_PIL = False

try:
    import fitz  # PyMuPDF
    HAY_FITZ = True
except ImportError:
    HAY_FITZ = False


# -------- Estado compartido --------
_lock = threading.Lock()
_sus_lock = threading.Lock()
items = []
indice = 0
suscriptores = []

_cache_pdf_lock = threading.Lock()
_cache_pdf_full = {}
_cache_pdf_thumb = {}


# -------- Listado de contenido (imagenes + PDFs) --------
def _contar_paginas_pdf(path):
    try:
        with fitz.open(str(path)) as doc:
            return doc.page_count
    except Exception:
        return 0


def listar_items():
    if not CARPETA_IMG.is_dir():
        return []
    resultado = []
    for f in sorted(CARPETA_IMG.iterdir(), key=lambda p: p.name.lower()):
        if not f.is_file():
            continue
        ext = f.suffix.lower()
        nombre_url = urllib.parse.quote(f.name)
        if ext in EXTENSIONES_IMG:
            resultado.append({
                'tipo': 'img',
                'nombre': f.name,
                'label': f.name,
                'src': f'/img/{nombre_url}',
                'thumb': f'/api/thumb/{nombre_url}',
            })
        elif ext in EXTENSIONES_PDF and HAY_FITZ:
            n = _contar_paginas_pdf(f)
            for i in range(n):
                resultado.append({
                    'tipo': 'pdf',
                    'nombre': f.name,
                    'pagina': i,
                    'label': f'{f.name} · {i + 1}/{n}',
                    'src': f'/pdf/{nombre_url}/{i}',
                    'thumb': f'/pdfthumb/{nombre_url}/{i}',
                })
    return resultado


def actualizar_lista():
    global items, indice
    nueva = listar_items()
    with _lock:
        cambio = len(nueva) != len(items) or any(a != b for a, b in zip(nueva, items))
        items = nueva
        if indice >= len(items):
            indice = 0
    if cambio:
        notificar()
    return items


def obtener_estado():
    with _lock:
        total = len(items)
        if total == 0:
            return {'index': 0, 'total': 0, 'src': None, 'label': None, 'tipo': None}
        it = items[indice]
        return {
            'index': indice,
            'total': total,
            'src': it['src'],
            'label': it['label'],
            'tipo': it['tipo'],
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
        total = len(items)
        if total:
            if nuevo < 0:
                nuevo = total - 1
            elif nuevo >= total:
                nuevo = 0
            indice = nuevo
    notificar()
    return obtener_estado()


# -------- Render PDF --------
def _render_pdf_pagina(path, pagina, dpi):
    try:
        with fitz.open(str(path)) as doc:
            if pagina < 0 or pagina >= doc.page_count:
                return None
            pix = doc[pagina].get_pixmap(dpi=dpi)
            return pix.tobytes('png')
    except Exception:
        return None


def _obtener_pdf_pagina(path, pagina, full):
    cache = _cache_pdf_full if full else _cache_pdf_thumb
    key = (str(path), pagina)
    with _cache_pdf_lock:
        data = cache.get(key)
        if data is not None:
            return data
    dpi = PDF_DPI_COMPLETA if full else PDF_DPI_MINIATURA
    data = _render_pdf_pagina(path, pagina, dpi)
    if data is None:
        return None
    with _cache_pdf_lock:
        if len(cache) > 200:      # cap simple de memoria
            cache.clear()
        cache[key] = data
    return data


# -------- HTML: visor (PC / OBS) --------
PAGINA_VISOR = r'''<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
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
    z-index:15;
  }
  #fs {
    position:fixed; bottom:16px; right:16px;
    width:46px; height:46px; border-radius:50%;
    background:rgba(255,255,255,.12); color:#fff;
    border:1px solid rgba(255,255,255,.3);
    font-size:20px; line-height:1; cursor:pointer;
    display:flex; align-items:center; justify-content:center;
    z-index:20; opacity:.45;
    transition:opacity .2s, background .2s;
    font-family:system-ui,sans-serif;
    -webkit-tap-highlight-color:transparent;
  }
  #fs:hover, #fs:active { opacity:1; background:rgba(255,255,255,.25); }
  body.clean #info, body.clean #fs { display:none; }
</style>
</head>
<body>
<div id="stage">
  <img id="capaA" class="capa" alt="">
  <img id="capaB" class="capa" alt="">
</div>
<div id="info"><span id="i">0</span> / <span id="t">0</span></div>
<button id="fs" type="button" title="Pantalla completa (F)" aria-label="Pantalla completa">⛶</button>
<script>
(function () {
  // Modo limpio para OBS: agregar ?clean a la URL
  if (new URLSearchParams(location.search).has('clean')) {
    document.body.classList.add('clean');
  }

  var capas = [document.getElementById('capaA'), document.getElementById('capaB')];
  var activa = 0;
  var actual = null;
  var estado = { index: 0, total: 0, src: null };

  function mostrar(src) {
    if (!src || src === actual) return;
    actual = src;
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
    img.src = src;
    if (img.complete) setTimeout(alCargar, 0);
  }

  function aplicar(est) {
    estado = est;
    document.getElementById('i').textContent = est.total ? est.index + 1 : 0;
    document.getElementById('t').textContent = est.total;
    mostrar(est.src);
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

  // --- Pantalla completa ---
  var fsBtn = document.getElementById('fs');

  function estaFullscreen() {
    return !!(document.fullscreenElement || document.webkitFullscreenElement || document.msFullscreenElement);
  }

  function entrarFullscreen() {
    var el = document.documentElement;
    var fn = el.requestFullscreen || el.webkitRequestFullscreen || el.msRequestFullscreen;
    if (fn) {
      try { var p = fn.call(el); if (p && p.catch) p.catch(function(){}); } catch (_) {}
    }
  }

  function salirFullscreen() {
    var fn = document.exitFullscreen || document.webkitExitFullscreen || document.msExitFullscreen;
    if (fn) {
      try { var p = fn.call(document); if (p && p.catch) p.catch(function(){}); } catch (_) {}
    }
  }

  function toggleFullscreen() {
    if (estaFullscreen()) salirFullscreen();
    else entrarFullscreen();
  }

  function sincronizarBoton() {
    var on = estaFullscreen();
    fsBtn.textContent = on ? '✕' : '⛶';
    fsBtn.title = on ? 'Salir de pantalla completa (Esc)' : 'Pantalla completa (F)';
    fsBtn.setAttribute('aria-label', fsBtn.title);
  }

  fsBtn.addEventListener('click', toggleFullscreen);
  document.addEventListener('fullscreenchange', sincronizarBoton);
  document.addEventListener('webkitfullscreenchange', sincronizarBoton);
  document.addEventListener('MSFullscreenChange', sincronizarBoton);

  document.addEventListener('keydown', function (e) {
    if (e.key === 'f' || e.key === 'F') { e.preventDefault(); toggleFullscreen(); return; }
    if (e.key === 'Escape') {
      // El navegador sale de fullscreen nativamente; forzamos por compatibilidad.
      if (estaFullscreen()) salirFullscreen();
      return;
    }
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


# -------- HTML: control (tablet / celular) --------
PAGINA_CONTROL = r'''<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">
<title>Control</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; -webkit-tap-highlight-color:transparent; }

  :root {
    --top-h: 52px;
    --bar-h: 88px;
    --pad: 8px;
    --gap: 8px;
    --min-col: 140px;
    --btn-fs: 24px;
    --btn-radius: 12px;
    --safe-bottom: env(safe-area-inset-bottom, 0px);
    --cell-h: 70px; /* ALTO FIJO SOLICITADO */
  }

  html, body {
    height:100%; background:#0b0b0d; color:#fff; overflow:hidden;
    font-family:system-ui,-apple-system,sans-serif;
    user-select:none; -webkit-user-select:none;
  }

  #top {
    padding:8px 12px; background:#000; height:var(--top-h);
    display:flex; gap:10px; justify-content:space-between; align-items:center;
    border-bottom:1px solid #232323;
  }
  #top h1 { font-size:15px; font-weight:500; letter-spacing:.3px; flex:1;
             white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  #top .num { font-size:13px; color:#9aa; font-variant-numeric:tabular-nums; }
  #b-ver {
    flex:0 0 auto; width:36px; height:36px; border-radius:50%;
    background:rgba(255,255,255,.08); color:#fff;
    border:1px solid rgba(255,255,255,.2);
    font-size:16px; cursor:pointer;
    display:flex; align-items:center; justify-content:center;
  }
  #b-ver:hover, #b-ver:active { background:rgba(255,255,255,.2); }
  #b-ver:disabled { opacity:.3; cursor:default; }

  #grid {
    position:absolute;
    top:var(--top-h);
    bottom:calc(var(--bar-h) + var(--safe-bottom));
    left:0; right:0;
    overflow-y:auto;
    padding:var(--pad);
    display:grid;
    gap:var(--gap);
    grid-template-columns:repeat(auto-fill, minmax(var(--min-col), 1fr));
    /* ESTA ES LA CLAVE PARA EVITAR EL SOLAPAMIENTO: Forzamos la altura de cada fila */
    grid-auto-rows: var(--cell-h);
    align-content:start;
    -webkit-overflow-scrolling:touch;
  }

  .cel {
    position: relative;
    width: 100%;
    height: var(--cell-h); /* Alto fijo exacto */
    background:#000;
    border-radius:6px;
    overflow:hidden;
    border:2px solid transparent;
    cursor:pointer;
    opacity:.65;
    transition:opacity .15s, border-color .15s, transform .1s;
  }

  /* La imagen ocupa el 100% de su caja de 70px sin deformarse */
  .cel img {
    width: 100%;
    height: 100%;
    object-fit: contain; 
    display: block;
  }

  /* La etiqueta flota abajo sin quitarle altura a la caja de 70px */
  .cel .cap {
    position: absolute;
    bottom: 0; left: 0; right: 0;
    background: rgba(0, 0, 0, 0.75);
    color: #ccc;
    font-size: 9px;
    text-align: center;
    padding: 2px 4px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    line-height: 1.2;
    pointer-events: none;
  }

  .cel.activa {
    border-color:#3b82f6; opacity:1;
    box-shadow:0 0 0 2px rgba(59,130,246,.35);
  }
  .cel:active { transform:scale(.97); }

  #barra {
    position:fixed; left:0; right:0; bottom:0;
    height:calc(var(--bar-h) + var(--safe-bottom));
    padding:8px;
    padding-bottom:calc(8px + var(--safe-bottom));
    background:#000; border-top:1px solid #232323;
    display:flex; gap:var(--gap);
  }
  #barra button {
    flex:1; background:#1b1e26; color:#fff; border:none;
    border-radius:var(--btn-radius); font-size:var(--btn-fs); cursor:pointer;
    transition:background .1s;
    min-width:0;
  }
  #barra button:active { background:#2d3342; }
  #barra button:disabled { opacity:.3; }

  #sinimg {
    position:absolute;
    top:var(--top-h);
    bottom:calc(var(--bar-h) + var(--safe-bottom));
    left:0; right:0;
    display:none; align-items:center; justify-content:center;
    color:#778; padding:20px; text-align:center; font-size:14px;
  }

  /* ---------- Preview pantalla completa ---------- */
  #preview {
    position:fixed; inset:0; background:#000; z-index:100;
    display:flex; align-items:center; justify-content:center;
    padding:env(safe-area-inset-top,0) env(safe-area-inset-right,0)
            env(safe-area-inset-bottom,0) env(safe-area-inset-left,0);
  }
  #preview[hidden] { display:none; }
  #preview-img {
    max-width:100%; max-height:100%; object-fit:contain;
    -webkit-user-drag:none;
  }
  #preview-close {
    position:absolute;
    top:calc(14px + env(safe-area-inset-top, 0px));
    right:calc(14px + env(safe-area-inset-right, 0px));
    width:46px; height:46px; border-radius:50%;
    background:rgba(255,255,255,.15); color:#fff;
    border:1px solid rgba(255,255,255,.3);
    font-size:20px; cursor:pointer;
    display:flex; align-items:center; justify-content:center;
  }

  /* ---------- Adaptación a celulares ---------- */
  @media (max-width: 520px) {
    :root {
      --top-h: 46px;
      --bar-h: 68px;
      --pad: 4px;
      --gap: 6px;
      --min-col: 120px;
      --btn-fs: 20px;
      --btn-radius: 10px;
    }
    #top { padding:6px 10px; gap:8px; }
    #top h1 { font-size:14px; }
    #top .num { font-size:12px; }
    #b-ver { width:32px; height:32px; font-size:14px; }
    #barra { padding:6px; }
    .cel { border-radius:4px; }
  }

  /* En vertical forzamos exactamente 2 columnas */
  @media (max-width: 520px) and (orientation: portrait) {
    #grid { grid-template-columns: repeat(2, 1fr); }
  }

  /* Teléfonos en horizontal (poca altura) */
  @media (max-height: 480px) {
    :root { --top-h: 40px; --bar-h: 60px; --btn-fs: 18px; --pad: 4px; --gap: 4px; }
    #top h1 { display:none; }
    #barra { padding:5px; }
  }
</style>
</head>
<body>
<div id="top">
  <h1>Control</h1>
  <span class="num"><span id="i">0</span> / <span id="t">0</span></span>
  <button id="b-ver" type="button" title="Ver pantalla completa" aria-label="Ver pantalla completa">⛶</button>
</div>
<div id="grid"></div>
<div id="sinimg">No hay imágenes ni PDFs en la carpeta <b>img/</b>.</div>
<div id="barra">
  <button id="b-prev" aria-label="Anterior">◀</button>
  <button id="b-first" aria-label="Primera">⏮</button>
  <button id="b-last" aria-label="Última">⏭</button>
  <button id="b-next" aria-label="Siguiente">▶</button>
</div>
<div id="preview" hidden>
  <img id="preview-img" alt="">
  <button id="preview-close" type="button" aria-label="Cerrar">✕</button>
</div>
<script>
(function () {
  var estado = { index: 0, total: 0, src: null };
  var celdas = [];
  var grid = document.getElementById('grid');
  var sinimg = document.getElementById('sinimg');
  var preview = document.getElementById('preview');
  var previewImg = document.getElementById('preview-img');
  var bVer = document.getElementById('b-ver');

  function estaFullscreen() {
    return !!(document.fullscreenElement || document.webkitFullscreenElement);
  }

  function cargarMiniaturas() {
    fetch('/api/images').then(function (r) { return r.json(); }).then(function (lista) {
      grid.innerHTML = ''; celdas = [];
      if (!lista.length) { sinimg.style.display = 'flex'; return; }
      sinimg.style.display = 'none';
      var frag = document.createDocumentFragment();
      lista.forEach(function (it, i) {
        var cel = document.createElement('div');
        cel.className = 'cel' + (it.tipo === 'pdf' ? ' slide' : '');
        cel.dataset.i = i;
        cel.title = it.label || '';
        var img = document.createElement('img');
        img.src = it.thumb;
        img.loading = 'lazy';
        img.alt = it.label || '';
        cel.appendChild(img);
        if (it.label) {
          var cap = document.createElement('span');
          cap.className = 'cap';
          cap.textContent = it.label;
          cel.appendChild(cap);
        }
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
    bVer.disabled = !estado.total;
    if (!preview.hidden && estado.src) previewImg.src = estado.src;
  }

  var ev = new EventSource('/api/events');
  ev.onmessage = function (e) {
    try { estado = JSON.parse(e.data); marcar(); } catch (_) {}
  };

  document.getElementById('b-prev').addEventListener('click', function () { irA(estado.index - 1); });
  document.getElementById('b-next').addEventListener('click', function () { irA(estado.index + 1); });
  document.getElementById('b-first').addEventListener('click', function () { irA(0); });
  document.getElementById('b-last').addEventListener('click', function () { irA(estado.total - 1); });

  function abrirPreview() {
    if (!estado.src) return;
    previewImg.src = estado.src;
    preview.hidden = false;
    var req = preview.requestFullscreen || preview.webkitRequestFullscreen;
    if (req) {
      try { var p = req.call(preview); if (p && p.catch) p.catch(function(){}); } catch (_) {}
    }
  }

  function cerrarPreview() {
    if (estaFullscreen()) {
      var ex = document.exitFullscreen || document.webkitExitFullscreen;
      if (ex) { try { var p = ex.call(document); if (p && p.catch) p.catch(function(){}); } catch (_) {} }
    }
    preview.hidden = true;
  }

  bVer.addEventListener('click', abrirPreview);
  document.getElementById('preview-close').addEventListener('click', function (e) {
    e.stopPropagation(); cerrarPreview();
  });
  previewImg.addEventListener('click', cerrarPreview);
  preview.addEventListener('click', function (e) {
    if (e.target === preview) cerrarPreview();
  });

  document.addEventListener('fullscreenchange', function () {
    if (!estaFullscreen() && !preview.hidden) preview.hidden = true;
  });
  document.addEventListener('webkitfullscreenchange', function () {
    if (!estaFullscreen() && !preview.hidden) preview.hidden = true;
  });

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && !preview.hidden) { cerrarPreview(); return; }
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
    server_version = "VisorPDF/1.1"

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

    def _servir_pdf_pagina(self, nombre, pagina, full):
        if not HAY_FITZ:
            self.send_error(503); return
        archivo = self._resolver(nombre)
        if archivo is None or archivo.suffix.lower() != '.pdf':
            self.send_error(404); return
        data = _obtener_pdf_pagina(archivo, pagina, full)
        if data is None:
            self.send_error(404); return
        self._enviar(data, 'image/png', cache='max-age=3600')

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

    def _parsear_pdf_path(self, prefijo, path):
        resto = path[len(prefijo):]
        partes = resto.rsplit('/', 1)
        if len(partes) != 2:
            return None, None
        nombre, pag_s = partes
        try:
            return nombre, int(pag_s)
        except ValueError:
            return None, None

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
                with _lock:
                    self._enviar_json(list(items))
            elif path == '/api/events':
                self._sse()
            elif path.startswith('/api/thumb/'):
                self._servir_thumb(path[len('/api/thumb/'):])
            elif path.startswith('/pdfthumb/'):
                nombre, pag = self._parsear_pdf_path('/pdfthumb/', path)
                if nombre is None:
                    self.send_error(404); return
                self._servir_pdf_pagina(nombre, pag, full=False)
            elif path.startswith('/pdf/'):
                nombre, pag = self._parsear_pdf_path('/pdf/', path)
                if nombre is None:
                    self.send_error(404); return
                self._servir_pdf_pagina(nombre, pag, full=True)
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
            with _lock:
                self._enviar_json({'items': list(items)})
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
        print(f'Se creó la carpeta "{CARPETA_IMG}". Coloca imágenes o PDFs dentro y vuelve a ejecutar.')
        return

    lista = actualizar_lista()
    if not lista:
        if CARPETA_IMG.is_dir():
            hay_pdfs = any(f.is_file() and f.suffix.lower() in EXTENSIONES_PDF
                           for f in CARPETA_IMG.iterdir())
            if hay_pdfs and not HAY_FITZ:
                print('Hay PDFs en la carpeta pero PyMuPDF no está instalado.')
                print('Instálalo con:   pip install PyMuPDF')
                return
        print(f'No hay imágenes ni PDFs en "{CARPETA_IMG}". Añade contenido y vuelve a ejecutar.')
        return

    ip = obtener_ip_local()
    pdfs = [i for i in lista if i['tipo'] == 'pdf']
    imgs = [i for i in lista if i['tipo'] == 'img']

    print('=' * 64)
    print("ViewerImg/PDF Stream versión 1.1")
    print("Basado en ViewerImg Stream - Totir Deloper - MIT License")
    print('=' * 64)
    print(f'Elementos: {len(lista)}  (imágenes: {len(imgs)}, diapositivas PDF: {len(pdfs)})')
    print(f'Pillow disponible:  {"sí" if HAY_PIL else "no (miniaturas de imágenes a tamaño original)"}')
    print(f'PyMuPDF disponible: {"sí" if HAY_FITZ else "no (los PDF se omiten — pip install PyMuPDF)"}')
    print()
    print(f'  Visor (PC / OBS):   http://localhost:{PUERTO}/')
    print(f'  Visor limpio OBS:   http://localhost:{PUERTO}/?clean')
    print(f'  Control (celular):  http://{ip}:{PUERTO}/control')
    print()
    print('  Atajos del visor: F = pantalla completa · Esc = salir ·')
    print('                    ←/→ = navegar · Home/End = primera/última.')
    print('=' * 64)
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
