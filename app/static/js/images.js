// The image list, and opening and saving the image on screen.
import { state, editable } from './state.js';
import { $, toast, updateButtons } from './ui.js';
import { getJSON, sendJSON } from './api.js';
import { changed, markSaved, resetHistory } from './history.js';
import { MIN_BOX, blankCanvas, draw, showImage } from './canvas.js';
import { loadCats } from './classes.js';

const list = $('side');
let rows = [];             // the list's row elements, by index
let activeRow = null;      // the highlighted row
let annotatedCount = 0;
let loadToken = 0;         // bumped by every openImage so stale responses are dropped

// fetch the list and build it; false if it couldn't be loaded
export async function loadList(){
  let d;
  try { d = await getJSON('api/images'); }
  catch(err){ toast('Could not load image list ('+err.message+')', true); return false; }
  // keep pointing at the same file even if the folder changed on disk
  const cur = state.images[state.idx] ? state.images[state.idx].file_name : null;
  const images = state.images = d.images;
  state.idx = cur ? images.findIndex(it=>it.file_name===cur) : -1;
  if (state.idx<0){ state.ready = false; state.dirty = false; }
  annotatedCount = d.annotated;
  showProgress();
  // built once per load, in one go; a single click handler on the list serves every row
  const frag = document.createDocumentFragment();
  rows = images.map((it,i)=>{
    const el = document.createElement('div');
    el.className = 'it'; el.dataset.i = i;
    const nm = document.createElement('span');
    nm.className = 'nm'; nm.textContent = it.file_name; nm.title = it.file_name;
    const done = document.createElement('span');
    done.className = 'done';
    el.append(nm, done);
    frag.appendChild(el);
    return el;
  });
  rows.forEach((_,i)=>markRow(i));
  activeRow = null;
  list.replaceChildren(frag);
  highlight();
  updateButtons();
  if (!images.length) list.innerHTML = '<div class="empty">No images yet. Use ＋ Import or drop them here.</div>';
  return true;
}
list.addEventListener('click', (e)=>{ const el = e.target.closest('.it'); if (el) openImage(+el.dataset.i); });

function markRow(i){
  const it = state.images[i];
  rows[i].lastChild.textContent = it.annotated ? '✔ '+it.box_count : '';
}

function showProgress(){
  $('progress').textContent = annotatedCount + ' / ' + state.images.length + ' annotated';
}

function highlight(){
  const el = rows[state.idx] || null;
  if (activeRow && activeRow!==el) activeRow.classList.remove('active');
  activeRow = el;
  if (el){ el.classList.add('active'); el.scrollIntoView({block:'nearest'}); }
}

// force: reload the image on screen, e.g. after labels were imported for it
export async function openImage(i, force){
  if (state.saving || i<0 || i>=state.images.length || (i===state.idx && state.ready && !force)) return;
  if (state.dirty && !confirm('You have unsaved boxes on this image. Discard them?')) return;
  const token = ++loadToken;
  Object.assign(state, {idx:i, ready:false, dirty:false, drag:null, sel:-1});
  const name = state.images[i].file_name;
  $('fname').textContent = name;
  highlight();
  updateButtons();
  // the boxes and the picture load side by side
  const next = new Image();
  const loaded = new Promise((ok, fail)=>{ next.onload = ok; next.onerror = ()=>fail(new Error('not an image the browser can show')); });
  next.src = 'api/images/'+encodeURIComponent(name);
  let ad;
  try { [ad] = await Promise.all([getJSON('api/annotations/'+encodeURIComponent(name)), loaded]); }
  catch(err){
    if (token!==loadToken) return;
    // don't leave the previous picture and its boxes on screen under this name
    state.boxes = [];
    blankCanvas();
    $('fname').textContent = name+' (can’t be shown)';
    $('boxcount').textContent = '—';
    updateButtons();
    $('cv').classList.remove('loading');
    toast('Could not load '+name+' ('+err.message+')', true);
    return;
  }
  // unpack the picture off the main thread where the browser can; Chromium refuses for very
  // big photos, and those unpack once when first drawn instead
  try { await next.decode(); } catch { /* decoded when first drawn instead */ }
  if (token!==loadToken) return;   // user already moved on to another image
  state.img = next;
  state.boxes = (ad.boxes||[]).map(b=>({x:b.x, y:b.y, w:b.w, h:b.h, category_id:b.category_id}));
  resetHistory();
  state.ready = true;
  showImage();
}

// resume where you left off: the first image that hasn't been saved yet
export function openFirstUnlabelled(){
  const first = state.images.findIndex(it=>!it.annotated);
  openImage(first>=0 ? first : 0);
}

// the image on screen was deleted: show nothing
export function forgetImage(){
  loadToken++;
  Object.assign(state, {ready:false, dirty:false, boxes:[], sel:-1, idx:-1});
  $('fname').textContent = '—';
  $('boxcount').textContent = '0 boxes';
  blankCanvas();
}

async function save(){
  if (!editable() || state.drag) return;
  const W = state.img.naturalWidth, H = state.img.naturalHeight;
  // clip each box to the image, then drop any that end up too small
  const clean = state.boxes.map(b=>{
    const x1 = Math.max(0,Math.min(b.x,W)), y1 = Math.max(0,Math.min(b.y,H));
    const x2 = Math.max(0,Math.min(b.x+b.w,W)), y2 = Math.max(0,Math.min(b.y+b.h,H));
    return {x:x1, y:y1, w:x2-x1, h:y2-y1, category_id:b.category_id};
  }).filter(b=>b.w>=MIN_BOX && b.h>=MIN_BOX);
  const name = state.images[state.idx].file_name;
  state.saving = true;
  updateButtons();
  let ok = false;
  try {
    const d = await getJSON('api/annotations/'+encodeURIComponent(name), sendJSON('PUT', {width:W, height:H, boxes:clean}));
    ok = true;
    // show what was saved: boxes clipped to the image, without the ones too small to keep
    if (JSON.stringify(clean) !== JSON.stringify(state.boxes)){ state.boxes = clean; state.sel = -1; changed(); }
    state.dirty = false;
    markSaved();
    toast('Saved '+d.saved_boxes+' box(es) ✔');
    // update just this row and the counter: rebuilding a list of thousands after every save was slow
    const i = state.images.findIndex(it=>it.file_name===name);
    if (i>=0){
      const it = state.images[i];
      if (!it.annotated){ it.annotated = true; annotatedCount++; }
      it.box_count = d.saved_boxes;
      markRow(i);
      showProgress();
    }
    await loadCats();   // class box counts
  } catch(err){ toast('Save failed ('+err.message+')', true); }
  state.saving = false;
  if (state.ready) draw(); else updateButtons();
  if (ok && state.idx>=0 && state.idx<state.images.length-1) openImage(state.idx+1);   // auto-advance
}

$('save').onclick = save;
$('prev').onclick = ()=>openImage(state.idx-1);
$('next').onclick = ()=>openImage(state.idx+1);
