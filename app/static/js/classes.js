// The classes panel: the list with each class's colour, key and box count; pick, add, rename, delete.
import { state, editable } from './state.js';
import { $, toast } from './ui.js';
import { getJSON, sendJSON } from './api.js';
import { changed, forgetEarlier } from './history.js';
import { colorOf, draw } from './canvas.js';

export async function loadCats(){
  try { state.cats = await getJSON('api/categories'); }
  catch(err){ toast('Could not load classes ('+err.message+')', true); return; }
  const { cats } = state;
  if (!cats.some(c=>c.id===state.activeCat)) state.activeCat = cats.length ? cats[0].id : null;
  renderCats();
  if (state.ready) draw();
}

function renderCats(){
  const list = $('cats');
  list.innerHTML = '';
  if (!state.cats.length){ list.innerHTML = '<div class="empty">Add a class to start drawing.</div>'; return; }
  const span = (className, text, title)=>{
    const el = document.createElement('span');
    el.className = className; el.textContent = text;
    if (title) el.title = title;
    return el;
  };
  const button = (text, title, onclick)=>{
    const el = document.createElement('button');
    el.className = 'ic'; el.textContent = text; el.title = title;
    el.onclick = (e)=>{ e.stopPropagation(); onclick(); };
    return el;
  };
  state.cats.forEach((c,i)=>{
    const el = document.createElement('div');
    el.className = 'cat' + (c.id===state.activeCat ? ' active' : '');
    const sw = span('sw', '');
    sw.style.background = colorOf(c.id);
    el.append(sw,
      span('key', i<9 ? i+1 : ''),
      span('cn', c.name, c.name),
      span('cc', c.box_count, 'saved boxes'),
      button('✎', 'Rename', ()=>renameCat(c)),
      button('✕', 'Delete (only if no box uses it)', ()=>deleteCat(c)));
    el.onclick = ()=>pickCat(c.id);
    list.appendChild(el);
  });
}

// pick the class for new boxes; a selected box switches to it too
export function pickCat(id){
  const { boxes, sel } = state;
  state.activeCat = id;
  if (editable() && !state.drag && sel>=0 && boxes[sel].category_id!==id){ boxes[sel].category_id = id; changed(); }
  renderCats();
  if (state.ready) draw();
}

$('addcat').onsubmit = async (e)=>{
  e.preventDefault();
  const input = $('catname');
  if (!input.value.trim()) return;
  try {
    const c = await getJSON('api/categories', sendJSON('POST', {name: input.value}));
    input.value = '';
    state.activeCat = c.id;
    await loadCats();
  } catch(err){ toast(err.message, true); }
};

async function renameCat(c){
  const name = prompt('Rename class "'+c.name+'" to:', c.name);
  if (name===null || !name.trim() || name.trim()===c.name) return;
  try { await getJSON('api/categories/'+c.id, sendJSON('PATCH', {name})); await loadCats(); }
  catch(err){ toast(err.message, true); }
}

async function deleteCat(c){
  // the server guards saved boxes; this guards the unsaved ones on screen
  if (state.boxes.some(b=>b.category_id===c.id)){ toast('"'+c.name+'" is used on this image; change or delete those boxes first', true); return; }
  try { await getJSON('api/categories/'+c.id, {method:'DELETE'}); }
  catch(err){ toast(err.message, true); return; }
  // undo could bring back a box of the deleted class, which a class added later (same id) would then take over
  if (state.ready) forgetEarlier();
  await loadCats();
}
