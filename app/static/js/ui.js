// Small helpers for the page: element lookup, messages, and which buttons can be used.
import { state, editable } from './state.js';
import { canUndo, canRedo } from './history.js';

export const $ = id => document.getElementById(id);

let toastTimer = null;
// a short message at the bottom of the page
export function toast(msg, isError){
  const t = $('toast');
  t.textContent = msg;
  t.style.background = isError ? '#dc2626' : '#16a34a';
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(()=>t.classList.remove('show'), 1600);
}

// enable only what can be done right now
export function updateButtons(){
  const { images, idx, boxes, sel, ready, saving } = state, can = editable();
  $('prev').disabled = saving || idx<=0;
  $('next').disabled = saving || idx<0 || idx>=images.length-1;
  $('del').disabled = !can || sel<0;
  $('clear').disabled = !can || !boxes.length;
  $('undo').disabled = !can || !canUndo();
  $('redo').disabled = !can || !canRedo();
  $('zoom').disabled = !ready;
  $('save').disabled = !can;
  $('cv').classList.toggle('loading', !ready);
}
