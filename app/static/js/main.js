// Start-up and keyboard shortcuts. Each part of the page is its own module:
//   state.js     what the parts share          canvas.js    drawing, zoom and pan, the mouse
//   history.js   undo / redo                   classes.js   the classes panel
//   ui.js        $, toast, button states       images.js    the image list, open and save
//   api.js       talking to the server         dialogs.js   Import, Export, Clear workspace
import { state } from './state.js';
import { deleteSelected, setSpace, undoRedo, view, zoomBy, zoomToFit } from './canvas.js';
import { loadCats, pickCat } from './classes.js';
import { loadList, openFirstUnlabelled, openImage } from './images.js';
import { openImport, refreshWorkspace } from './dialogs.js';

document.addEventListener('keydown',(e)=>{
  if (e.target.closest && e.target.closest('input, textarea')) return;   // typing a class name
  if (document.querySelector('dialog[open]')) return;                    // import/export window on top
  const mod = e.ctrlKey || e.metaKey, key = e.key.length===1 ? e.key.toLowerCase() : e.key;
  if ((key==='Delete'||key==='Backspace') && state.sel>=0){ e.preventDefault(); deleteSelected(); }
  else if (mod && key==='z' && !e.shiftKey){ e.preventDefault(); undoRedo(-1); }
  else if (mod && (key==='y' || (key==='z' && e.shiftKey))){ e.preventDefault(); undoRedo(1); }
  else if (mod || e.altKey) return;                                      // leave the browser's own shortcuts alone
  else if (key==='ArrowLeft' || key==='ArrowRight'){ e.preventDefault(); openImage(state.idx + (key==='ArrowRight' ? 1 : -1)); }
  else if (key===' '){ e.preventDefault(); setSpace(true); }             // preventDefault: a focused button isn't pressed
  else if (!state.ready) return;
  else if (key==='+' || key==='=') zoomBy(1.25);
  else if (key==='-') zoomBy(1/1.25);
  else if (key==='0') zoomToFit();
  else if (/^[1-9]$/.test(key) && state.cats[key-1]) pickCat(state.cats[key-1].id);
});
document.addEventListener('keyup',(e)=>{ if (e.key===' '){ setSpace(false); e.preventDefault(); } });
window.addEventListener('blur', ()=>setSpace(false));
window.addEventListener('beforeunload',(e)=>{ if (state.dirty){ e.preventDefault(); e.returnValue=''; } });

window.annotator = { state, view };   // for the browser tests, and a look from the console

refreshWorkspace();
Promise.all([loadCats(), loadList()]).then(([, listed])=>{
  if (!listed) return;
  if (state.images.length) openFirstUnlabelled();
  else openImport();   // an empty workspace starts with importing
});
