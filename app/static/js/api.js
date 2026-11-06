// Talking to the server.

// the server's reason (e.g. "a class called X already exists") when it gives one
async function errorText(r){
  try {
    const d = await r.json();
    if (typeof d.detail==='string') return d.detail;
    // a validation error: a list of {loc, msg}, e.g. "images › 0 › width: Field required"
    if (Array.isArray(d.detail)) return d.detail.map(e=>(e.loc||[]).slice(1).join(' › ')+': '+e.msg).join('; ');
  } catch { /* not JSON: fall back to the status code */ }
  return 'HTTP '+r.status;
}

// the response's JSON; a failed request throws an Error with the server's reason and the HTTP status
export async function getJSON(url, opts){
  const r = await fetch(url, opts);
  if (!r.ok) throw Object.assign(new Error(await errorText(r)), {status: r.status});
  return r.status===204 ? null : r.json();
}

// fetch options that send `body` as JSON
export const sendJSON = (method, body) => ({method, headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
