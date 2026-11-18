// code belongs to: Vincent Jovan Wijaya
// Work is serialized: one Python interpreter owns decoded data and the ZIP writer.
let runtime;
async function initialize() {
  postMessage({progress:'Loading Python runtime…'});
  const runtimeUrl = new URL('vendor/pyodide/', import.meta.url);
  const {loadPyodide} = await import(new URL('pyodide.mjs', runtimeUrl).href);
  const py = await loadPyodide({indexURL: runtimeUrl.href});
  postMessage({progress:'Loading scientific libraries…'});
  await py.loadPackage(['numpy','scipy']);
  py.FS.mkdirTree('/app/decoders'); py.FS.mkdirTree('/input');
  for (const wheel of ['openpyxl-3.1.5-py2.py3-none-any.whl','et_xmlfile-2.0.0-py3-none-any.whl']) {
    const r=await fetch(new URL('vendor/'+wheel,import.meta.url));if(!r.ok)throw Error('Cannot load Excel dependency');
    py.unpackArchive(await r.arrayBuffer(),'zip',{extractDir:'/app'});
  }
  const files=['curve_io.py','resonance_engine.py','vcpnt_common.py','web_bridge.py','decoders/__init__.py','decoders/vcpnt_sine.py','decoders/vcpnt_random.py','decoders/vcpnt_shock.py'];
  await Promise.all(files.map(async name=>{const r=await fetch(new URL('python/'+name,import.meta.url));if(!r.ok)throw Error('Cannot load '+name);py.FS.writeFile('/app/'+name,await r.text());}));
  await py.runPythonAsync("import sys\nsys.path.insert(0, '/app')\nfrom web_bridge import handle");
  return py;
}
let queue=Promise.resolve();
onmessage=({data})=>{queue=queue.then(async()=>{
  let py, path;
  try {
    runtime ||= initialize(); py=await runtime;
    const req={...data.request};
    if(data.bytes){path='/input/'+String(data.id)+'_'+req.name.replace(/[^a-zA-Z0-9._-]/g,'_');py.FS.writeFile(path,new Uint8Array(data.bytes));req.path=path;}
    let result;
    if(req.op==='init')result=true;
    else {py.globals.set('_request',JSON.stringify(req));result=JSON.parse(await py.runPythonAsync('handle(_request)'));}
    // Show original display names, not worker temporary paths.
    if(req.op==='load')result.name=req.name;
    postMessage({id:data.id,result});
  }catch(e){postMessage({id:data.id,error:String(e.message||e)});}
  finally{if(path&&py)try{py.FS.unlink(path);}catch{}}
});};
