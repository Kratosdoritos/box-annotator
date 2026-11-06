// Undo / redo: a copy of the boxes after every change; pos is the one on screen.
import { state } from './state.js';

const LIMIT = 200;
let stack = [], pos = 0, savedPos = 0;

const snapshot = () => JSON.stringify(state.boxes.map(({x,y,w,h,category_id})=>({x,y,w,h,category_id})));

// a newly opened image: its boxes are what's saved
export function resetHistory(){
  stack = [snapshot()];
  pos = savedPos = 0;
}

// call after each edit; the image counts as unsaved unless undone back to what was saved
export function changed(){
  stack.length = pos + 1;   // a new edit drops what could be redone
  stack.push(snapshot());
  pos++;
  if (stack.length > LIMIT){ stack.shift(); pos--; savedPos--; }
  state.dirty = pos !== savedPos;
}

// -1 = undo, 1 = redo; false when there's nothing to go back or forward to
export function step(n){
  if (pos+n < 0 || pos+n >= stack.length) return false;
  pos += n;
  state.boxes = JSON.parse(stack[pos]);
  state.dirty = pos !== savedPos;
  return true;
}

// a class was deleted: earlier copies may still have boxes of it, so they can't be gone back to
export function forgetEarlier(){
  const saved = pos === savedPos;
  stack = [snapshot()];
  pos = 0;
  savedPos = saved ? 0 : -1;   // -1: what was saved is no longer in the history, so it stays unsaved
}

export function markSaved(){ savedPos = pos; }
export const canUndo = () => pos > 0;
export const canRedo = () => pos < stack.length - 1;
