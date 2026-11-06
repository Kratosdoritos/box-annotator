// What the parts of the UI share. One object, so every module reads and changes the same values.
export const state = {
  images: [],            // the image list: {file_name, annotated, box_count}
  idx: -1,               // the image on screen, as an index into images
  img: new Image(),      // its picture
  boxes: [],             // its boxes, in image px: {x,y,w,h,category_id}
  cats: [],              // the dataset's classes {id,name,box_count}, in id order
  activeCat: null,       // class id given to new boxes
  sel: -1,               // the selected box, as an index into boxes
  drag: null,            // a mouse drag in progress: {mode:'new'|'move'|'resize'|'pan', moved, ...}
  ready: false,          // the image is loaded and `boxes` belong to it
  dirty: false,          // unsaved edits on the image
  saving: false,
};

// the boxes on screen can be changed
export const editable = () => state.ready && !state.saving;
