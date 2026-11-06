// The canvas: drawing the image and its boxes, zoom and pan, and editing boxes with the mouse.
import { state, editable } from './state.js';
import { $, toast, updateButtons } from './ui.js';
import { changed, step } from './history.js';

const cv = $('cv');
const ctx = cv.getContext('2d');
const HANDLE = 8;            // px in screen space
export const MIN_BOX = 3;    // px in image space; smaller boxes are discarded

// which part of the image is on screen, and how big
export const view = {
  fit: 1,                    // canvas CSS px per image px with the whole image in view
  zoom: 1,                   // 1 = the whole image; more magnifies
  scale: 1,                  // canvas CSS px per image px: fit × zoom
  panX: 0, panY: 0,          // the image px at the canvas's top-left corner
  w: 0, h: 0,                // canvas size in CSS px
};
let base = null;             // the image already scaled to the canvas; a redraw just copies it
let drawQueued = false;
let spaceDown = false;       // Space held: dragging pans instead of drawing
let grabbed = false;         // the selected box was picked by clicking it, so dragging it moves it

// picked to stand out on photos and greyscale scans; a class keeps its colour by id
const COLORS = ['#22c55e','#f97316','#06b6d4','#e11d48','#a855f7','#eab308','#3b82f6','#84cc16','#ec4899','#14b8a6'];
export const colorOf = id => state.cats.some(c=>c.id===id) ? COLORS[((id-1) % COLORS.length + COLORS.length) % COLORS.length] : '#94a3b8';   // ids can start at 0
const catName = id => (state.cats.find(c=>c.id===id) || {name:'?'}).name;

// size the canvas for the image on screen; a window resize keeps the zoom
export function fitCanvas(){
  const { img } = state;
  const stage = document.querySelector('.stage');
  const maxW = stage.clientWidth - 20, maxH = stage.clientHeight - 20;
  view.fit = Math.min(maxW/img.naturalWidth, maxH/img.naturalHeight, 1);
  if (!isFinite(view.fit) || view.fit<=0) view.fit = 1;
  view.w = Math.max(1, Math.round(img.naturalWidth*view.fit));
  view.h = Math.max(1, Math.round(img.naturalHeight*view.fit));
  // back the canvas at device resolution so the image stays sharp on HiDPI screens
  const dpr = window.devicePixelRatio || 1;
  cv.style.width = view.w+'px'; cv.style.height = view.h+'px';
  cv.width = Math.round(view.w*dpr); cv.height = Math.round(view.h*dpr);
  ctx.setTransform(dpr,0,0,dpr,0,0);
  // scale the picture down once here: redrawing a big photo at every mouse move made dragging stutter
  base = document.createElement('canvas');
  base.width = cv.width; base.height = cv.height;
  const bctx = base.getContext('2d');
  bctx.imageSmoothingQuality = 'high';
  bctx.drawImage(img, 0, 0, base.width, base.height);
  setView(view.zoom, view.panX, view.panY);
}
window.addEventListener('resize', ()=>{ if (state.ready){ fitCanvas(); draw(); } });

// a newly opened image, all of it in view
export function showImage(){
  view.zoom = 1; view.panX = view.panY = 0;
  fitCanvas();
  draw();
}

// nothing on screen, e.g. for an image the browser can't show
export function blankCanvas(){
  cv.width = cv.height = 0;
  cv.style.width = cv.style.height = '0px';
}

// ---- zoom and pan ------------------------------------------------------------------
const maxZoom = ()=>Math.max(1, 8/view.fit);   // up to 8 screen px per image px

// set the zoom and the view's corner, keeping the view inside the image
function setView(z, px, py){
  const { img } = state;
  view.zoom = Math.min(Math.max(z, 1), maxZoom());
  view.scale = view.fit*view.zoom;
  view.panX = Math.max(0, Math.min(px, img.naturalWidth - view.w/view.scale));
  view.panY = Math.max(0, Math.min(py, img.naturalHeight - view.h/view.scale));
  $('zoom').textContent = Math.round(view.zoom*100)+'%';
}

// zoom so the image point under (sx, sy) stays under it
function zoomAt(z, sx, sy){
  const ix = sx/view.scale + view.panX, iy = sy/view.scale + view.panY;
  const nz = Math.min(Math.max(z, 1), maxZoom());
  setView(nz, ix - sx/(view.fit*nz), iy - sy/(view.fit*nz));
  requestDraw();
}
// zoom in (factor > 1) or out around the middle of the view
export function zoomBy(factor){ zoomAt(view.zoom*factor, view.w/2, view.h/2); }
// back to the whole image
export function zoomToFit(){ setView(1, 0, 0); requestDraw(); }

cv.addEventListener('wheel',(e)=>{
  if (!state.ready) return;
  e.preventDefault();   // the page itself doesn't scroll
  const dy = e.deltaMode===1 ? e.deltaY*33 : e.deltaMode===2 ? e.deltaY*800 : e.deltaY;   // lines or pages -> px
  const {sx,sy} = mouse(e);
  zoomAt(view.zoom*Math.exp(-dy*0.002), sx, sy);
},{passive:false});
cv.addEventListener('mousedown', e=>{ if (e.button===1) e.preventDefault(); });   // no autoscroll on middle-drag
$('zoom').onclick = ()=>{ if (state.ready) zoomToFit(); };

// Space+drag pans
export function setSpace(down){
  spaceDown = down;
  cv.classList.toggle('pan', down);
}

// ---- drawing -----------------------------------------------------------------------
// at most one redraw per screen frame, however fast the mouse events come
function requestDraw(){
  if (drawQueued) return;
  drawQueued = true;
  requestAnimationFrame(()=>{ drawQueued = false; if (state.ready) draw(); });
}

export function draw(){
  const { img, boxes, sel } = state, { zoom, scale, panX, panY, w: viewW, h: viewH } = view;
  ctx.clearRect(0,0,viewW,viewH);
  if (zoom===1) ctx.drawImage(base,0,0,viewW,viewH);
  else ctx.drawImage(img, panX, panY, viewW/scale, viewH/scale, 0, 0, viewW, viewH);   // just the part in view
  ctx.font = '12px system-ui, sans-serif';
  boxes.forEach((b,i)=>{
    const x=(b.x-panX)*scale, y=(b.y-panY)*scale, w=b.w*scale, h=b.h*scale;
    const col = colorOf(b.category_id);
    ctx.lineWidth = (i===sel) ? 3 : 2;
    ctx.strokeStyle = col;
    ctx.strokeRect(x,y,w,h);
    // class name on a tab above the box (inside it when there's no room)
    const label = catName(b.category_id), lw = ctx.measureText(label).width + 8, ly = y>=16 ? y-16 : y;
    ctx.fillStyle = col; ctx.fillRect(x-1, ly, lw, 16);
    ctx.fillStyle = '#0b1120'; ctx.fillText(label, x+3, ly+12);
    if (i===sel){ // resize handles
      ctx.lineWidth = 2;
      [[x,y],[x+w,y],[x,y+h],[x+w,y+h]].forEach(([hx,hy])=>{
        ctx.fillStyle='#fff'; ctx.fillRect(hx-HANDLE/2,hy-HANDLE/2,HANDLE,HANDLE);
        ctx.strokeRect(hx-HANDLE/2,hy-HANDLE/2,HANDLE,HANDLE);
      });
    }
  });
  $('boxcount').textContent = boxes.length + ' box' + (boxes.length===1?'':'es');
  updateButtons();
}

// ---- the mouse ---------------------------------------------------------------------
function mouse(e){
  const r = cv.getBoundingClientRect();
  // client px -> canvas CSS px, even if CSS has shrunk the canvas
  return { sx:(e.clientX-r.left)*view.w/r.width, sy:(e.clientY-r.top)*view.h/r.height };
}
// screen -> image px, clamped so boxes can't be drawn or dragged off the image
function imgCoords(sx,sy){
  const { img } = state, { scale, panX, panY } = view;
  return { x:Math.max(0,Math.min(img.naturalWidth, sx/scale+panX)), y:Math.max(0,Math.min(img.naturalHeight, sy/scale+panY)) };
}
// a box's rectangle on screen
function onScreen(b){
  const { scale, panX, panY } = view;
  return { x:(b.x-panX)*scale, y:(b.y-panY)*scale, w:b.w*scale, h:b.h*scale };
}

// the selected box's corner under the point: 'tl', 'tr', 'bl' or 'br'. Inside the box the corners
// take less room the smaller it is: otherwise a box under two handles wide is all corners and can't be moved
function handleAt(sx,sy){
  if (state.sel<0) return null;
  const {x,y,w,h} = onScreen(state.boxes[state.sel]);
  const inside = sx>x && sx<x+w && sy>y && sy<y+h;
  const reach = inside ? Math.min(HANDLE, w/4, h/4) : HANDLE;
  const pts = {tl:[x,y], tr:[x+w,y], bl:[x,y+h], br:[x+w,y+h]};
  for (const k in pts){
    const [hx,hy] = pts[k];
    if (Math.abs(sx-hx)<=reach && Math.abs(sy-hy)<=reach) return k;
  }
  return null;
}
// the smallest box under the point, so a box drawn inside a bigger one can still be picked
function boxAt(sx,sy){
  const { boxes } = state;
  let best = -1;
  boxes.forEach((b,i)=>{
    const {x,y,w,h} = onScreen(b);
    if (sx>=x && sx<=x+w && sy>=y && sy<=y+h && (best<0 || b.w*b.h < boxes[best].w*boxes[best].h)) best = i;
  });
  return best;
}

cv.addEventListener('pointerdown',(e)=>{
  if (!state.ready || state.drag) return;
  const {sx,sy} = mouse(e);
  if (e.button===1 || (e.button===0 && spaceDown)){   // pan: drag with the middle button, or hold Space
    e.preventDefault(); cv.setPointerCapture(e.pointerId);
    state.drag = {mode:'pan', sx, sy, panX:view.panX, panY:view.panY};
    cv.classList.add('panning');
    return;
  }
  if (!editable() || e.button!==0) return;
  cv.setPointerCapture(e.pointerId);
  const { boxes, sel } = state;
  const corner = handleAt(sx,sy);
  if (corner){ state.drag = {mode:'resize', corner, start:imgCoords(sx,sy), orig:{...boxes[sel]}}; return; }
  const hit = boxAt(sx,sy);
  // only a box picked by clicking moves when dragged: dragging inside any other box draws a
  // new one, so drawing a small box inside a big one never shifts the big one by accident
  if (hit>=0 && hit===sel && grabbed){ state.drag = {mode:'move', start:imgCoords(sx,sy), orig:{...boxes[sel]}}; return; }
  if (state.activeCat===null){
    if (hit>=0){ state.sel = hit; grabbed = true; draw(); return; }
    toast('Add a class first (left panel)', true);
    $('catname').focus();
    return;
  }
  // new box, in the active class (a plain click selects the box under it instead, in endDrag)
  const p = imgCoords(sx,sy);
  state.drag = {mode:'new', start:p, hit};
  boxes.push({x:p.x, y:p.y, w:0, h:0, category_id:state.activeCat});
  state.sel = boxes.length-1; grabbed = false;
  draw();
});

cv.addEventListener('pointermove',(e)=>{
  const { drag } = state;
  if (!drag) return;
  const {sx,sy} = mouse(e);
  if (drag.mode==='pan'){
    setView(view.zoom, drag.panX-(sx-drag.sx)/view.scale, drag.panY-(sy-drag.sy)/view.scale);
    requestDraw();
    return;
  }
  const p = imgCoords(sx,sy), b = state.boxes[state.sel];
  const W = state.img.naturalWidth, H = state.img.naturalHeight;
  if (p.x!==drag.start.x || p.y!==drag.start.y) drag.moved = true;
  if (drag.mode==='new'){
    b.x = Math.min(drag.start.x,p.x); b.y = Math.min(drag.start.y,p.y);
    b.w = Math.abs(p.x-drag.start.x); b.h = Math.abs(p.y-drag.start.y);
  } else if (drag.mode==='move'){
    const dx = p.x-drag.start.x, dy = p.y-drag.start.y;
    b.x = Math.max(0,Math.min(W-b.w,drag.orig.x+dx)); b.y = Math.max(0,Math.min(H-b.h,drag.orig.y+dy));
  } else if (drag.mode==='resize'){
    let {x,y,w,h} = drag.orig, x2 = x+w, y2 = y+h;
    if (drag.corner.includes('l')) x = p.x;
    if (drag.corner.includes('r')) x2 = p.x;
    if (drag.corner.includes('t')) y = p.y;
    if (drag.corner.includes('b')) y2 = p.y;
    b.x = Math.min(x,x2); b.y = Math.min(y,y2); b.w = Math.abs(x2-x); b.h = Math.abs(y2-y);
  }
  requestDraw();
});

function endDrag(){
  const { drag, boxes } = state;
  if (!drag) return;
  state.drag = null;
  if (drag.mode==='pan'){ cv.classList.remove('panning'); return; }
  const b = boxes[state.sel];
  if (drag.mode!=='move' && (b.w<MIN_BOX || b.h<MIN_BOX)){   // a click, or a box squashed flat
    boxes.splice(state.sel,1); state.sel = -1;
    if (drag.mode==='resize') changed();
    else if (drag.hit>=0){ state.sel = drag.hit; grabbed = true; }    // a click on a box selects it, ready to move
  } else if (drag.moved) changed();
  draw();
}
cv.addEventListener('pointerup', endDrag);
cv.addEventListener('pointercancel', endDrag);

// ---- edits from the toolbar and the keyboard ---------------------------------------
export function deleteSelected(){
  if (!editable() || state.drag || state.sel<0) return;
  state.boxes.splice(state.sel,1);
  state.sel = -1;
  changed();
  draw();
}

function clearBoxes(){
  if (!editable() || state.drag || !state.boxes.length) return;
  state.boxes = [];
  state.sel = -1;
  changed();
  draw();
}

// -1 = undo, 1 = redo
export function undoRedo(n){
  if (!editable() || state.drag || !step(n)) return;
  state.sel = -1;
  draw();
}

$('del').onclick = deleteSelected;
$('clear').onclick = clearBoxes;
$('undo').onclick = ()=>undoRedo(-1);
$('redo').onclick = ()=>undoRedo(1);
