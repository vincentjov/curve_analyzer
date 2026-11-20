// code belongs to: Vincent Jovan Wijaya
const $=id=>document.getElementById(id);
const records=[]; let activeId=null,recordCounter=0,homeEntries=[];
const state={mode:'home',meta:null,m:null,index:0,panels:[],analysis:null,files:[],busy:false,stop:false};
const blank=()=>({title:'',quantity:'',names:[],curveLabels:new Map(),logx:true,logy:false});
const sourceChannelName = name => name.replace(/ \(STEST\)$/, '');
const originalPlotTitle = index => 'Plot ' + (index + 1);
state.panels=Array.from({length:4},blank);
let worker,requestId=0,pending=new Map(),syncing=false;
function status(text,error=false){$('status').textContent=text;$('status').classList.toggle('error',error);}
function element(tag,text,cls){const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(cls)el.className=cls;return el;}
function option(select,value,text=value){const o=element('option',text);o.value=value;select.append(o);}
function startEngine(){worker?.terminate();for(const p of pending.values())p.reject(Error('Engine restarted.'));pending.clear();worker=new Worker(new URL('worker.js?v=1.2',import.meta.url),{type:'module'});
 worker.onmessage=({data})=>{if(data.progress){status(data.progress);return;}const p=pending.get(data.id);if(!p)return;pending.delete(data.id);data.error?p.reject(Error(data.error)):p.resolve(data.result);};
 worker.onerror=e=>{for(const p of pending.values())p.reject(Error(e.message||'Could not start engine.'));pending.clear();$('retryEngine').hidden=false;};
 $('retryEngine').hidden=true;$('engineState').textContent='Engine loading';
 request({op:'init'}).then(()=>{$('engineState').textContent='Python engine ready · Files processed locally';status('Ready. Open a recording or choose a workflow.');}).catch(e=>{$('engineState').textContent='Engine unavailable';$('retryEngine').hidden=false;status('Engine could not load. Check that the site’s vendor folder was published completely, then retry. '+e.message,true);});
}
function request(req,bytes){if(activeId&&!('record_id' in req))req={...req,record_id:activeId};return new Promise((resolve,reject)=>{const id=++requestId;pending.set(id,{resolve,reject});worker.postMessage({id,request:req,bytes},bytes?[bytes]:[]);});}
async function task(fn){if(state.busy)return;state.busy=true;$('plotTools').inert=true;document.body.classList.add('busy');try{await fn();}catch(e){status(e.message,true);}finally{state.busy=false;$('plotTools').inert=false;document.body.classList.remove('busy');}}
function mode(name) {
  stashRecord();
  state.mode = name;
  for (const button of document.querySelectorAll('.workspace-nav button')) {
    const active = button.dataset.mode === name;
    button.classList.toggle('active', active);
    if (active) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  }
  $('home').hidden = name !== 'home';
  $('batch').hidden = name !== 'batch';
  $('workspace').hidden = !['plot', 'analyze'].includes(name);
  $('analysisTools').hidden = name !== 'analyze';
  $('pageTitle').textContent = {home:'Your data. A clearer view.',plot:'Plot & save',analyze:'Analyze curve',batch:'Save multiple files'}[name];
  if (!$('workspace').hidden) renderWorkspace();
}
document.querySelectorAll('[data-mode]').forEach(b=>b.onclick=()=>chooseMode(b.dataset.mode));document.querySelector('.brand').onclick=e=>{e.preventDefault();mode('home');};
$('retryEngine').onclick=()=>location.reload();
function selectedMeasurement(){return Number($('measurement').value)||0;}
// Each recording keeps its own measurement, panels, sensor choices and results.
const controlIds=['plotCount','layout','response','reference','linkX','wheelY','axisLow','axisHigh','axisStep','axisLog'];
function stashRecord(){const record=records.find(r=>r.id===activeId);if(!record)return;record.saved={meta:state.meta,m:state.m,index:state.index,panels:state.panels,analysis:state.analysis,controls:Object.fromEntries(controlIds.map(id=>[id,$(id).type==='checkbox'?$(id).checked:$(id).value])),thresholds:[...document.querySelectorAll('[data-setting]')].map(el=>[el.dataset.setting,el.value])};if(!state.analysis)delete record.visuals.analyze;}
function renderTabs(){$('recordTabs').replaceChildren();for(const record of records){const wrap=element('div',undefined,'record-tab'+(record.id===activeId?' active':'')),button=element('button',record.file.name);button.setAttribute('role','tab');button.setAttribute('aria-selected',record.id===activeId?'true':'false');button.title=record.relative;button.onclick=()=>task(()=>activateRecord(record));const close=element('button','×','close-tab');close.setAttribute('aria-label','Close '+record.file.name);close.onclick=()=>task(()=>closeRecord(record));wrap.append(button,close);$('recordTabs').append(wrap);}}
async function activateRecord(record){stashRecord();activeId=record.id;const saved=record.saved;Object.assign(state,{meta:saved.meta,m:saved.m,index:saved.index,panels:saved.panels,analysis:saved.analysis});$('fileName').textContent=record.relative;$('measurement').replaceChildren();state.meta.measurements.forEach((m,i)=>option($('measurement'),i,m));$('measurement').disabled=false;$('measurement').value=state.index;updateAxis();populateSensors();for(const [id,value] of Object.entries(saved.controls||{})){if($(id).type==='checkbox')$(id).checked=value;else $(id).value=value;}for(const [key,value] of saved.thresholds||[])document.querySelector('[data-setting="'+key+'"]').value=value;renderTabs();renderWorkspace();}
async function closeRecord(record){const index=records.indexOf(record);await request({op:'close',record_id:record.id});records.splice(index,1);if(activeId===record.id){activeId=null;if(records.length)await activateRecord(records[Math.min(index,records.length-1)]);else{Object.assign(state,{meta:null,m:null,index:0,analysis:null,panels:Array.from({length:4},blank)});$('measurement').replaceChildren();$('measurement').disabled=true;$('fileName').textContent='No recording selected';$('axisNote').hidden=true;$('axisSettings').hidden=true;$('response').replaceChildren();$('reference').replaceChildren();renderWorkspace();}}renderTabs();}
async function openRecordings(entries){let opened=0,failed=[];for(const entry of entries){if(!supported(entry.file))continue;const key=entry.relative+'|'+entry.file.size+'|'+entry.file.lastModified;const existing=records.find(r=>r.key===key);if(existing){await activateRecord(existing);continue;}try{await loadFile(entry.file,entry.relative,key);opened++;}catch(e){failed.push(entry.file.name+': '+e.message);}}status(`${opened} recording(s) added · ${records.length} open`+(failed.length?' · Failed: '+failed.join('; '):''),!!failed.length);}
async function loadFile(file,relative=file.name,key=relative+'|'+file.size+'|'+file.lastModified){stashRecord();const id='record-'+(++recordCounter);status('Decoding '+file.name+'…');const meta=await request({op:'load',record_id:id,name:file.name},await file.arrayBuffer());const record={id,file,relative,key,visuals:{},saved:{}};records.push(record);activeId=id;state.meta=meta;state.analysis=null;state.panels=Array.from({length:4},blank);$('plotCount').value='1';$('layout').value='grid';$('fileName').textContent=relative;$('measurement').replaceChildren();meta.measurements.forEach((m,i)=>option($('measurement'),i,m));$('measurement').disabled=false;state.index=meta.measurements.length-1;$('measurement').value=state.index;updateAxis();await loadMeasurement(true);stashRecord();renderTabs();if(!['plot','analyze'].includes(state.mode))mode('plot');}
async function chooseMode(name){if(state.busy)return;if(homeEntries.length&&name!=='home'){const entries=homeEntries;homeEntries=[];renderHomeSelection();if(name==='batch'){addFiles(entries);mode(name);}else{mode(name);await task(()=>openRecordings(entries));}}else mode(name);}
function updateAxis(){$('axisSettings').hidden=false;$('axisNote').hidden=state.meta.axis_verified;$('axisNote').textContent=state.meta.axis_note+' Supply the actual test settings below before analysis or export.';}
async function loadMeasurement(openSetup = false){const record=records.find(r=>r.id===activeId);if(record)record.visuals={};state.index=selectedMeasurement();state.m=await request({op:'measurement',index:state.index});state.analysis=null;
 for(const p of state.panels)p.names=p.names.map(n=>state.m.curves.find(c=>sourceChannelName(c.name)===sourceChannelName(n)&&c.quantity===p.quantity)?.name).filter(Boolean);
 populateSensors();renderWorkspace(openSetup);}
function populateSensors(){
 for(const id of ['response','reference']){$(id).replaceChildren();state.m.curves.filter(c=>c.eligible).forEach(c=>option($(id),c.name));}
 const eligible=state.m.curves.filter(c=>c.eligible);const response=eligible.find(c=>/response|R_[A-Z]_2/i.test(c.name))||eligible[0];const reference=eligible.find(c=>c.name!==response?.name&&/control|^C\d/i.test(c.name))||eligible.find(c=>c!==response);
 if(response)$('response').value=response.name;if(reference)$('reference').value=reference.name;
 $('thresholds').replaceChildren();for(const [key,value] of Object.entries(state.meta.settings)){const label=element('label',key.replaceAll('_',' '));let input;if(key==='smoothing_points'){input=element('select');[1,5,7,9,11].forEach(v=>option(input,v,v===1?'Off':v+' points'));}else{input=element('input');input.type='number';input.step='any';}input.value=value;input.dataset.setting=key;label.append(input);$('thresholds').append(label);}
}
$('openFile').onclick=()=>$('fileInput').click();$('fileInput').onchange=e=>{const files=[...e.target.files];if(files.length)task(()=>openRecordings(files.map(file=>({file,relative:file.name}))));e.target.value='';};$('measurement').onchange=()=>task(loadMeasurement);
$('applyAxis').onclick=()=>task(async()=>{if(!state.meta)throw Error('Open a recording first.');const values=state.meta.kind==='sine'?{low:Number($('axisLow').value),high:Number($('axisHigh').value),logarithmic:$('axisLog').checked}:{step:Number($('axisStep').value)};const name=state.meta.name;state.meta=await request({op:'override',values});state.meta.name=name;updateAxis();await loadMeasurement();status('X-axis test settings applied.');});
function renderWorkspace(openSetup = false) {
  const showSetup = state.mode === 'plot' && Boolean(state.m);
  $('plotTools').hidden = !showSetup;
  if (showSetup) {
    const panels = state.panels.slice(0, Number($('plotCount').value));
    if (openSetup || !panels.some(panel => panel.names.length)) $('plotTools').open = true;
    buildEditor(openSetup ? 0 : undefined);
  } else {
    $('plotEditors').replaceChildren();
  }
  renderGraphs();
}

function openPlotEditor(index) {
  $('plotTools').open = true;
  const editor = $('plotEditor' + index);
  editor.open = true;
  editor.querySelector('summary').focus();
}

function defaultPlotQuantity(curves) {
  const accelerationUnits = state.meta.kind === 'random' ? 'g²/Hz' : 'g';
  const preferred = curves.find(curve => curve.y_unit === accelerationUnits);
  return preferred?.quantity ?? curves[0]?.quantity ?? '';
}

function setPlotQuantity(panel, quantity) {
  panel.quantity = quantity;
  panel.names = [];
  panel.logx = state.meta.kind === 'sine';
  panel.logy = state.m.curves.some(curve => curve.quantity === quantity && curve.log_y);
}

const sensorOrder = new Intl.Collator('en', {numeric: true, sensitivity: 'base'});

function curveGroups(curves) {
  const controls = [], responses = new Map(), limits = [], other = [];
  for (const curve of curves) {
    const name = curve.name.replace(/(?:\s+\(STEST\)|\s+\[\d+\])+$/i, '').trim();
    const compact = name.replace(/[\s_-]+/g, '');
    if (curve.role === 'reference' || /^(reference|alarm|abort|limit)\b/i.test(name)) {
      limits.push(curve);
      continue;
    }
    if (/^(control|ctrl|steuer)/i.test(compact) || /^C\d+[XYZ]?$/i.test(compact)) {
      controls.push(curve);
      continue;
    }

    // In Response_X1(Z), the parenthesized Z is the physical axis.
    // Older R_A_1 labels identify a sensor but do not identify an axis.
    const match = /^(?:triacc|response|resp|accelerometer|acc|r)([xyz]?)(\d+)(?:\(([xyz])\)|([xyz]))?$/i.exec(compact);
    const legacy = /^R_[A-Z]+_(\d+)$/i.exec(name);
    if (match || legacy) {
      const number = Number(match?.[2] ?? legacy[1]);
      const axis = (match?.[3] || match?.[4] || match?.[1] || '').toUpperCase();
      if (!responses.has(number)) responses.set(number, {X: [], Y: [], Z: [], other: []});
      responses.get(number)[axis || 'other'].push(curve);
    } else {
      other.push(curve);
    }
  }

  const byName = (left, right) => sensorOrder.compare(left.name, right.name);
  controls.sort((left, right) => {
    const number = curve => Number(curve.name.match(/\d+/)?.[0] ?? -1);
    return number(left) - number(right) || byName(left, right);
  });
  return {controls, responses: [...responses].sort(([a], [b]) => a - b), limits, other:other.sort(byName)};
}

function curveCheckbox(curve, panel, onChange, label = curve.name, accessibleLabel = label) {
  const choice = element('label', undefined, 'curve-choice');
  const checkbox = element('input');
  checkbox.type = 'checkbox';
  checkbox.checked = panel.names.includes(curve.name);
  checkbox.setAttribute('aria-label', accessibleLabel);
  choice.title = curve.name;
  checkbox.onchange = () => {
    panel.names = checkbox.checked
      ? [...panel.names, curve.name]
      : panel.names.filter(name => name !== curve.name);
    onChange();
  };
  choice.append(checkbox, element('span', label));
  return choice;
}

function sensorRow(title, contents, axisRow = false) {
  const row = element('div', undefined, 'sensor-row');
  const heading = element('h4', title);
  const cells = element('div', undefined, axisRow ? 'sensor-axes' : 'sensor-choices');
  cells.append(...contents);
  row.append(heading, cells);
  return row;
}

function renderCurveChoices(container, panel, onChange) {
  const checkbox = (curve, label, accessibleLabel) => curveCheckbox(curve, panel, onChange, label, accessibleLabel);
  const curves = state.m.curves.filter(curve => curve.quantity === panel.quantity);
  const {controls, responses, limits, other} = curveGroups(curves);
  const rows = [];
  if (controls.length) rows.push(sensorRow('Control', controls.map(c => checkbox(c))));

  for (const [number, channels] of responses) {
    const title = 'Response ' + number;
    const axes = ['X', 'Y', 'Z'].map(axis => {
      const cell = element('div', undefined, 'sensor-axis');
      const matches = channels[axis];
      if (!matches.length) {
        cell.append(element('span', axis + ' —', 'axis-unavailable'));
      } else {
        for (const curve of matches) {
          const label = matches.length === 1 ? axis : axis + ' · ' + curve.name;
          cell.append(checkbox(curve, label, title + ' ' + axis + ' · ' + curve.name));
        }
      }
      return cell;
    });
    const row = sensorRow(title, axes, true);
    if (channels.other.length) {
      const unnamedAxes = element('div', undefined, 'sensor-choices sensor-unknown-axis');
      unnamedAxes.append(...channels.other.map(c => checkbox(c)));
      row.append(unnamedAxes);
    }
    rows.push(row);
  }

  if (limits.length) rows.push(sensorRow('Reference & limits', limits.map(c => checkbox(c))));
  if (other.length) rows.push(sensorRow('Other curves', other.map(c => checkbox(c))));
  container.replaceChildren(...rows);
}

function channelCount(panel) {
  const count = panel.names.length;
  return count ? count + (count === 1 ? ' channel' : ' channels') : 'Select channels';
}

function buildEditor(openIndex) {
  const editors = $('plotEditors');
  const count = Number($('plotCount').value);
  const previousIndex = Number(editors.querySelector('.plot-editor[open]')?.dataset.index ?? 0);
  const activeIndex = openIndex ?? Math.min(previousIndex, count - 1);
  const quantities = [...new Set(state.m.curves.map(curve => curve.quantity))];
  const sections = [];
  for (let i = 0; i < count; i++) {
    const panel = state.panels[i];
    if (!quantities.includes(panel.quantity)) setPlotQuantity(panel, defaultPlotQuantity(state.m.curves));

    const section = element('details', undefined, 'plot-editor');
    section.id = 'plotEditor' + i;
    section.dataset.index = i;
    section.setAttribute('name', 'plot-editors');
    section.open = i === activeIndex;
    const summary = element('summary');
    const countLabel = element('span', channelCount(panel), 'plot-channel-count');
    summary.append(element('strong', originalPlotTitle(i)), countLabel);
    section.append(summary);

    const body = element('div', undefined, 'plot-editor-body');
    const quantityLabel = element('label', 'Quantity');
    const quantity = element('select');
    quantities.forEach(value => option(quantity, value));
    quantity.value = panel.quantity;
    quantityLabel.append(quantity);

    const settings = element('details', undefined, 'plot-settings');
    settings.append(element('summary', 'Title & axis scales'));
    const fields = element('div', undefined, 'plot-settings-fields');
    const titleLabel = element('label', 'Title');
    const title = element('input');
    title.id = 'plotTitle' + i;
    title.value = panel.title;
    title.placeholder = originalPlotTitle(i);
    title.onchange = () => updatePlotTitle(i, title.value);
    titleLabel.append(title);
    fields.append(titleLabel);

    const scales = {};
    const scaleFields = element('div', undefined, 'plot-scales');
    for (const [key, text] of [['logx', 'Log X'], ['logy', 'Log Y']]) {
      const label = element('label', undefined, 'check');
      const input = element('input');
      input.type = 'checkbox';
      input.checked = panel[key];
      input.onchange = () => {
        panel[key] = input.checked;
        renderPlot(i);
      };
      scales[key] = input;
      label.append(input, document.createTextNode(text));
      scaleFields.append(label);
    }
    fields.append(scaleFields);
    settings.append(fields);

    const choices = element('div', undefined, 'sensor-list');
    const updateChannels = () => {
      countLabel.textContent = channelCount(panel);
      renderPlot(i);
      updateGraphActions();
    };
    quantity.onchange = () => {
      setPlotQuantity(panel, quantity.value);
      for (const key of Object.keys(scales)) scales[key].checked = panel[key];
      renderCurveChoices(choices, panel, updateChannels);
      updateChannels();
    };
    renderCurveChoices(choices, panel, updateChannels);
    if (!state.m.curves.length) choices.append(element('p', 'No channels are available in this measurement.', 'plot-instructions'));
    body.append(quantityLabel, choices, settings);
    section.append(body);
    sections.push(section);
  }
  editors.replaceChildren(...sections);
}

$('plotCount').onchange = () => {
  buildEditor();
  renderGraphs();
};
$('layout').onchange = renderGraphs;

const colors=['#3779dc','#e78d4a','#7768c6','#34a590','#cf648c','#77963c','#34a8c7','#b28d5c'];
function trace(c,i){return {x:c.x,y:c.y,name:c.name,type:'scatter',mode:'lines',line:{width:1.5,color:colors[i%colors.length]},connectgaps:false};}
function showGraphCard(card, index) {
  const previous = $('graphs').children[index];
  if (!previous) {
    $('graphs').append(card);
    return;
  }
  const chart = previous.querySelector('.chart');
  if (chart) window.Plotly?.purge(chart);
  previous.replaceWith(card);
}

function chartCard(traces,title,xlabel,ylabel,logx=false,logy=false,plot){
 const index=plot?.index;
 const card=element('div',undefined,'graph-card'),div=element('div',undefined,'chart');card.append(div);showGraphCard(card,index);if(!window.Plotly){card.replaceChildren(element('p','Graph library unavailable. Check that the local Plotly file was published and reload.','notice'));return;}
 const layout={title:{text:title,font:{size:14,color:'#4a5b74'},x:.05},margin:{l:75,r:25,t:50,b:65},paper_bgcolor:'#fff',plot_bgcolor:'#fff',font:{family:'-apple-system, BlinkMacSystemFont, Segoe UI, sans-serif',size:10,color:'#73829a'},xaxis:{title:xlabel,type:logx?'log':'linear',gridcolor:'#eef1f6',zerolinecolor:'#e7edf5'},yaxis:{title:ylabel,type:logy?'log':'linear',gridcolor:'#eef1f6',zerolinecolor:'#e7edf5'},legend:{orientation:'h',y:-.22,font:{size:plot?11:9}},hovermode:'closest',dragmode:'zoom',showlegend:true};
 const visuals=records.find(r=>r.id===activeId)?.visuals[state.mode];
 const snapshot=visuals&&{data:structuredClone(traces),layout:structuredClone(layout)};
 if(visuals){
   if(index===undefined)visuals.push(snapshot);
   else visuals[index]=snapshot;
 }
 Plotly.newPlot(div,traces,layout,{responsive:true,displaylogo:false,scrollZoom:false,edits:{titleText:!!plot,legendText:!!plot},modeBarButtonsToRemove:['lasso2d','select2d'],toImageButtonOptions:{format:'png',scale:2}}).then(()=>{
 if(!div.isConnected)return;
 if(plot)bindPlotLabels(div,plot,snapshot);
 div.on('plotly_relayout',event=>{if(syncing||!$('linkX').checked||!('xaxis.range[0]'in event||'xaxis.range'in event||'xaxis.autorange'in event))return;syncing=true;const updates=Object.fromEntries(Object.entries(event).filter(([k])=>k.startsWith('xaxis.')));Promise.all([...document.querySelectorAll('.chart')].filter(other=>other!==div&&other._fullLayout?.xaxis.type===div._fullLayout.xaxis.type&&other.dataset.xunit===div.dataset.xunit).map(other=>Plotly.relayout(other,updates))).finally(()=>syncing=false);});
 div.addEventListener('wheel',event=>{if(!$('wheelY').checked&&!event.shiftKey&&!event.ctrlKey)return;event.preventDefault();const axis=event.shiftKey?'xaxis':'yaxis',full=div._fullLayout[axis];if(!full)return;const range=full.range,ratio=event.deltaY>0?1.18:1/1.18,rect=div.getBoundingClientRect();const pixel=axis==='xaxis'?event.clientX-rect.left:event.clientY-rect.top;let fraction=(pixel-full._offset)/full._length;if(axis==='yaxis')fraction=1-fraction;fraction=Math.max(0,Math.min(1,fraction));const center=range[0]+fraction*(range[1]-range[0]);Plotly.relayout(div,{[axis+'.range']:[center+(range[0]-center)*ratio,center+(range[1]-center)*ratio],[axis+'.autorange']:false});},{passive:false});
 });div.dataset.xunit=xlabel;return div;}
function purgeGraphs(){document.querySelectorAll('.chart').forEach(d=>window.Plotly?.purge(d));$('graphs').replaceChildren();}
function updateGraphActions() {
  $('graphActions').hidden = state.mode === 'plot' && !$('graphs').querySelector('.chart');
  if (state.mode === 'plot') {
    $('graphHint').textContent = 'Click a title or legend label to rename it. Use the legend swatch to show or hide a curve.';
  }
}

function rememberPlotTitle(index, value) {
  const original = originalPlotTitle(index);
  const title = value.trim();
  const panel = state.panels[index];
  panel.title = title === original ? '' : title;
  const sidebarTitle = $('plotTitle' + index);
  if (sidebarTitle) sidebarTitle.value = panel.title;
  return panel.title || original;
}

function updatePlotTitle(index, value) {
  const title = rememberPlotTitle(index, value);
  const chart = $('graphs').children[index]?.querySelector('.chart');
  if (chart?._fullLayout) {
    Plotly.relayout(chart, {'title.text':title});
  } else {
    const inlineTitle = $('inlinePlotTitle' + index);
    if (inlineTitle) inlineTitle.value = title;
  }
}

function curveLabelKey(curve) {
  return JSON.stringify([curve.quantity, sourceChannelName(curve.name)]);
}

const editablePlotLabels = '.gtitle, .legendtext';
function bindPlotLabels(chart, {index, panel, curves}, snapshot) {
  const makeLabelsFocusable = () => {
    for (const label of chart.querySelectorAll(editablePlotLabels)) {
      label.setAttribute('tabindex', '0');
      label.setAttribute('role', 'button');
      const kind = label.classList.contains('gtitle') ? 'plot' : 'curve';
      label.setAttribute('aria-label', 'Rename ' + kind + ' ' + label.textContent.trim());
    }
  };
  makeLabelsFocusable();
  chart.on('plotly_afterplot', makeLabelsFocusable);
  chart.addEventListener('keydown', event => {
    if (!['Enter', ' '].includes(event.key) || !event.target.matches(editablePlotLabels)) return;
    event.preventDefault();
    event.target.dispatchEvent(new MouseEvent('click', {bubbles:true}));
  });

  chart.on('plotly_relayout', event => {
    const edited = event['title.text'];
    if (typeof edited !== 'string') return;
    const title = rememberPlotTitle(index, edited);
    if (snapshot) snapshot.layout.title.text = title;
    if (edited !== title) Plotly.relayout(chart, {'title.text':title});
  });

  chart.on('plotly_restyle', ([changes, indices]) => {
    if (!Object.hasOwn(changes, 'name')) return;
    const labels = indices.map(traceIndex => {
      const curve = curves[traceIndex];
      const label = chart.data[traceIndex].name.trim() || curve.name;
      const key = curveLabelKey(curve);
      if (label === curve.name) panel.curveLabels.delete(key);
      else panel.curveLabels.set(key, label);
      if (snapshot) snapshot.data[traceIndex].name = label;
      return label;
    });
    if (labels.some((label, i) => label !== chart.data[indices[i]].name)) {
      Plotly.restyle(chart, {name:labels}, indices);
    }
  });
}

function renderPlot(index) {
  const panel = state.panels[index];
  const title = panel.title || originalPlotTitle(index);
  const curves = state.m.curves.filter(curve => curve.quantity === panel.quantity && panel.names.includes(curve.name));
  if (curves.length) {
    const traces = curves.map((curve, i) => ({
      ...trace(curve, i),
      name: panel.curveLabels.get(curveLabelKey(curve)) ?? curve.name
    }));
    chartCard(traces, title, curves[0].x_unit, curves[0].y_unit, panel.logx, panel.logy, {index, panel, curves});
    return;
  }

  const visuals = records.find(record => record.id === activeId)?.visuals.plot;
  if (visuals) delete visuals[index];
  const card = element('div', undefined, 'graph-card empty-chart');
  const inlineTitle = element('input', undefined, 'inline-plot-title');
  inlineTitle.type = 'text';
  inlineTitle.id = 'inlinePlotTitle' + index;
  inlineTitle.value = title;
  inlineTitle.setAttribute('aria-label', 'Rename ' + originalPlotTitle(index));
  inlineTitle.onfocus = () => inlineTitle.select();
  inlineTitle.onchange = () => updatePlotTitle(index, inlineTitle.value);
  inlineTitle.onkeydown = event => {
    if (event.key === 'Escape') inlineTitle.value = panel.title || originalPlotTitle(index);
    if (event.key === 'Escape' || event.key === 'Enter') inlineTitle.blur();
  };
  card.append(inlineTitle);
  if (state.m.curves.length) {
    card.append(element('span', 'Select channels to display in this plot.'));
    const button = element('button', 'Select channels');
    button.setAttribute('aria-label', 'Select channels for Plot ' + (index + 1));
    button.onclick = () => openPlotEditor(index);
    card.append(button);
  } else {
    card.append(element('span', 'No channels are available in this measurement.'));
  }
  showGraphCard(card, index);
}

function renderGraphs() {
  const record = records.find(r => r.id === activeId);
  if (record && ['plot', 'analyze'].includes(state.mode)) record.visuals[state.mode] = [];
  purgeGraphs();
  $('results').hidden = true;
  if (state.mode === 'analyze') {
    updateGraphActions();
    renderAnalysis();
    return;
  }
  if (state.mode !== 'plot') return;

  const count = Number($('plotCount').value);
  $('graphs').className = 'graphs ' + ({1:'one',2:'two',3:'three'}[count] || '') + ($('layout').value === 'stack' ? ' stack' : '');
  if (state.m) {
    for (let i = 0; i < count; i++) renderPlot(i);
  } else {
    $('graphs').className = 'graphs one';
    const card = element('div', undefined, 'graph-card empty-chart');
    card.append(
      element('strong', 'Open a recording to get started'),
      element('span', 'Then choose how many plots to show and select channels in Edit plots.')
    );
    $('graphs').append(card);
  }
  updateGraphActions();
}

let graphWidth = 0;
new ResizeObserver(([entry]) => {
  const width = entry.contentRect.width;
  if (width === graphWidth) return;
  graphWidth = width;
  if (!width) return;
  for (const chart of $('graphs').querySelectorAll('.chart')) {
    if (chart._fullLayout) window.Plotly.Plots.resize(chart).catch(() => {});
  }
}).observe($('graphArea'));

$('runAnalysis').onclick=()=>task(async()=>{if(!state.m)throw Error('Open a sine recording first.');if($('response').value===$('reference').value)throw Error('Choose different response and reference sensors.');state.analysis=null;renderGraphs();status('Screening resonance candidates…');const settings={};document.querySelectorAll('[data-setting]').forEach(el=>settings[el.dataset.setting]=Number(el.value));state.analysis=await request({op:'analyze',index:state.index,response:$('response').value,reference:$('reference').value,settings});if(state.mode==='analyze')renderGraphs();status('Screening finished. Review candidate reasons below the graphs.');});
// Changing a sensor or threshold invalidates displayed results and their export.
for(const id of ['response','reference','thresholds'])$(id).addEventListener('change',()=>{state.analysis=null;if(state.mode==='analyze')renderGraphs();});
function renderAnalysis(){$('graphs').className='graphs stack';$('graphHint').textContent='Response / reference screening · Select a peak to highlight it';if(!state.analysis){const c=element('div',state.meta&&state.meta.kind!=='sine'?'Resonance screening is available for sine recordings only.':'Choose response and reference sensors, then find resonance.','graph-card empty-chart');$('graphs').append(c);return;}const a=state.analysis;chartCard([trace({x:a.x,y:a.response,name:a.response_name},0),trace({x:a.x,y:a.reference,name:a.reference_name},1)],'Acceleration','Hz','g',true,true);chartCard([trace({x:a.x,y:a.amplification,name:'Response / reference'},0),{x:a.peaks.map(p=>p.frequency_hz),y:a.peaks.map(p=>p.amplification),text:a.peaks.map(p=>p.status),name:'Candidates',type:'scatter',mode:'markers',marker:{size:9,color:a.peaks.map(p=>p.status.startsWith('Accepted')?'#21977b':'#d8993d')}}],'Transmissibility','Hz','×',true,false);chartCard([trace({x:a.x,y:a.phase,name:'Relative phase'},2)],'Relative phase','Hz','°',true,false);
 $('results').hidden=false;$('results').replaceChildren();const accepted=a.peaks.filter(p=>p.status.startsWith('Accepted')).length;$('results').append(element('h3',`${accepted} accepted · ${a.peaks.length-accepted} candidates for review`));if(a.notes.length)$('results').append(element('p',a.notes.join(' · '),'notice'));const wrap=element('div',undefined,'results-wrap'),table=element('table'),head=element('thead'),hr=element('tr');['Frequency (Hz)','Status','Amplification','Phase change (°)','Decision / notes'].forEach(t=>hr.append(element('th',t)));head.append(hr);table.append(head);const body=element('tbody');a.peaks.forEach(p=>{const row=element('tr');[p.frequency_hz.toFixed(3),p.status,p.amplification.toFixed(3),p.phase_change_deg?.toFixed(1)??'Unavailable',[...p.reasons,...p.notes].join('; ')].forEach((t,i)=>row.append(element('td',t,i===1?(p.status.startsWith('Accepted')?'accepted':'candidate'):'')));row.tabIndex=0;const select=()=>{body.querySelectorAll('tr').forEach(r=>r.classList.remove('selected'));row.classList.add('selected');document.querySelectorAll('.chart').forEach(d=>Plotly.relayout(d,{shapes:[{type:'line',xref:'x',yref:'paper',x0:p.frequency_hz,x1:p.frequency_hz,y0:0,y1:1,line:{color:'#d8993d',width:1,dash:'dot'}}]}));};row.onclick=select;row.onkeydown=e=>{if(e.key==='Enter')select();};body.append(row);});table.append(body);wrap.append(table);$('results').append(wrap);}
$('resetZoom').onclick=()=>document.querySelectorAll('.chart').forEach(d=>window.Plotly?.relayout(d,{'xaxis.autorange':true,'yaxis.autorange':true}));
function closeExpanded(){$('graphArea').classList.remove('expanded');$('graphArea').querySelector('.close-expanded')?.remove();window.dispatchEvent(new Event('resize'));}
$('expandGraph').onclick=()=>{$('graphArea').classList.add('expanded');const close=element('button','✕ Close expanded view','close-expanded');close.onclick=closeExpanded;$('graphArea').prepend(close);window.dispatchEvent(new Event('resize'));};document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!e.target.closest?.('.plugin-editable, .inline-plot-title'))closeExpanded();});
function download(result){const bytes=Uint8Array.from(atob(result.data),c=>c.charCodeAt(0));const url=URL.createObjectURL(new Blob([bytes]));const a=element('a');a.href=url;a.download=result.name;a.click();setTimeout(()=>URL.revokeObjectURL(url),60000);}
$('exportData').onclick=()=>task(async()=>{if(!state.m)throw Error('Open a recording first.');if(!state.meta.axis_verified)throw Error('Supply X-axis settings before export.');const analysis=state.mode==='analyze';if(analysis&&!state.analysis)throw Error('Run analysis first.');const names=[...new Set(state.panels.slice(0,Number($('plotCount').value)).flatMap(p=>p.names))];if(!analysis&&!names.length)throw Error('Select channels in Edit plots first.');download(await request({op:'export',index:state.index,names,analysis,format:$('dataFormat').value}));status('Download prepared.');});
$('exportGraph').onclick=()=>task(async()=>{const graphs=[...document.querySelectorAll('.chart')];if(!graphs.length)throw Error('Draw a graph first.');for(let i=0;i<graphs.length;i++)await Plotly.downloadImage(graphs[i],{format:$('graphFormat').value,filename:'curve_plot_'+(i+1),width:1400,height:800,scale:2});status('Graph downloads prepared. Your browser may ask to allow multiple downloads.');});$('printGraphs').onclick=()=>window.print();
const supported=f=>/\.(rsn|rrn|rcs|xlsx|xlsm)$/i.test(f.name);
function addFiles(entries){let skipped=0;for(const item of entries){if(!supported(item.file)){skipped++;continue;}const key=item.relative+'|'+item.file.size+'|'+item.file.lastModified;if(!state.files.some(x=>x.key===key))state.files.push({...item,key});}renderQueue();status(`${state.files.length} recording(s) queued.${skipped?' '+skipped+' unsupported files skipped.':''}`);}
function renderQueue(){$('queueCount').textContent=state.files.length+' recording(s) queued';$('queue').replaceChildren();state.files.forEach((item,i)=>{const row=element('div',undefined,'queue-row'),name=element('span',item.relative),remove=element('button','×');remove.setAttribute('aria-label','Remove '+item.relative);remove.onclick=()=>{state.files.splice(i,1);renderQueue();};row.append(name,element('small',(item.file.size/1024).toFixed(0)+' KB'),remove);$('queue').append(row);});}
$('chooseFiles').onclick=()=>$('batchInput').click();$('chooseFolder').onclick=()=>$('folderInput').click();for(const id of ['batchInput','folderInput'])$(id).onchange=e=>{addFiles([...e.target.files].map(file=>({file,relative:file.webkitRelativePath||file.name})));e.target.value='';};$('clearQueue').onclick=()=>{state.files=[];renderQueue();};
async function readEntry(entry,prefix=''){if(entry.isFile)return new Promise((resolve,reject)=>entry.file(file=>resolve([{file,relative:prefix+file.name}]),reject));if(entry.isDirectory){const reader=entry.createReader();let children=[],chunk;do{chunk=await new Promise((resolve,reject)=>reader.readEntries(resolve,reject));children.push(...chunk);}while(chunk.length);const results=[];for(const child of children)results.push(...await readEntry(child,prefix+entry.name+'/'));return results;}return [];}
const zone=$('dropZone');zone.onclick=()=>$('batchInput').click();zone.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();$('batchInput').click();}};
window.addEventListener('dragover',e=>{e.preventDefault();});window.addEventListener('drop',e=>{e.preventDefault();});zone.ondragover=e=>{e.preventDefault();zone.classList.add('over');};zone.ondragleave=()=>zone.classList.remove('over');zone.ondrop=e=>{e.preventDefault();zone.classList.remove('over');if(state.busy)return;const entries=[...e.dataTransfer.items].map(i=>i.webkitGetAsEntry?.()).filter(Boolean);const files=[...e.dataTransfer.files];task(async()=>{if(entries.length){let all=[];for(const entry of entries)all.push(...await readEntry(entry));addFiles(all);}else addFiles(files.map(file=>({file,relative:file.name})));status('Dropped recordings added. Conversion downloads one ZIP; your browser controls its save location.');});};
$('convert').onclick=()=>task(async()=>{if(!state.files.length)throw Error('Choose recordings or a folder first.');state.stop=false;$('stopBatch').hidden=false;$('batchLog').textContent='';const files=[...state.files],format=$('batchFormat').value;let ok=0,fail=0,done=0;const lines=[];try{await request({op:'batch_start'});for(const item of files){if(state.stop)break;status(`Converting ${done+1} of ${files.length}: ${item.file.name}`);try{const result=await request({op:'batch_add',name:item.file.name,relative:item.relative,format},await item.file.arrayBuffer());ok++;lines.push(`OK · ${item.relative} · ${result.measurements} measurement(s)`);}catch(e){fail++;lines.push(`FAILED · ${item.relative} · ${e.message}`);}done++;$('batchLog').textContent=lines.join('\n');}lines.push(`\n${ok} converted, ${fail} failed, ${files.length-done} not processed.`);download(await request({op:'batch_finish',report:lines.join('\n')}));$('batchLog').textContent=lines.join('\n');status('ZIP download prepared. '+lines.at(-1).trim());}finally{$('stopBatch').hidden=true;}});$('stopBatch').onclick=()=>{state.stop=true;status('Stopping after the current recording…');};
$('demo').onclick=()=>task(async()=>{const r=await fetch('examples/GENERATED_sine.rsn');if(!r.ok)throw Error('Example unavailable.');await loadFile(new File([await r.arrayBuffer()],'GENERATED_sine.rsn'));});
startEngine();

// Detached windows receive only a snapshot of plots, never recording access.
$('detachGraph').onclick=()=>{
 const charts=[...document.querySelectorAll('.chart')].filter(d=>d.data&&d.layout);
 if(!charts.length)return status('Draw a graph first.',true);
 const snapshot=JSON.parse(JSON.stringify(charts.map(d=>({data:d.data,layout:d.layout}))));
 const popup=window.open('detached.html','_blank','popup,width=1150,height=850');
 if(!popup)return status('Allow pop-ups for this site to detach graphs, or use Expand.',true);
 const listener=e=>{if(e.origin===location.origin&&e.source===popup&&e.data?.type==='curve-detached-ready'){popup.postMessage({type:'curve-snapshot',graphs:snapshot},location.origin);window.removeEventListener('message',listener);}};
 window.addEventListener('message',listener);setTimeout(()=>window.removeEventListener('message',listener),60000);
};
// code belongs to: Vincent Jovan Wijaya — flexible intake before workflow selection.
function renderHomeSelection(){$('homeSelection').textContent=homeEntries.length?homeEntries.length+' recording(s) ready — choose a workflow below':'No recordings selected';}
function stageHome(entries){let skipped=0;for(const entry of entries){if(!supported(entry.file)){skipped++;continue;}if(!homeEntries.some(x=>x.relative===entry.relative&&x.file.size===entry.file.size&&x.file.lastModified===entry.file.lastModified))homeEntries.push(entry);}renderHomeSelection();status(homeEntries.length+' recording(s) ready. Choose Plot, Analyze or Save multiple files.'+(skipped?' '+skipped+' unsupported files skipped.':''));}
$('homeDrop').onclick=()=>$('homePicker').showModal();$('homeDrop').onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();$('homePicker').showModal();}};
for(const [button,input] of [['homeFiles','homeFileInput'],['homeFolder','homeFolderInput'],['pickerFiles','homeFileInput'],['pickerFolder','homeFolderInput']])$(button).onclick=()=>{$('homePicker').close();$(input).click();};
$('pickerCancel').onclick=()=>$('homePicker').close();$('clearHome').onclick=()=>{homeEntries=[];renderHomeSelection();};
for(const id of ['homeFileInput','homeFolderInput'])$(id).onchange=e=>{stageHome([...e.target.files].map(file=>({file,relative:file.webkitRelativePath||file.name})));e.target.value='';};
$('workspaceDrop').onclick=()=>$('fileInput').click();$('workspaceDrop').onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();$('fileInput').click();}};
for(const id of ['homeDrop','workspaceDrop']){$(id).ondragover=()=>$(id).classList.add('over');$(id).ondragleave=()=>$(id).classList.remove('over');}
window.addEventListener('drop',e=>{
 $('homeDrop').classList.remove('over');$('workspaceDrop').classList.remove('over');
 if(zone.contains(e.target)||state.busy)return;
 const entries=[...e.dataTransfer.items].map(i=>i.webkitGetAsEntry?.()).filter(Boolean),files=[...e.dataTransfer.files],targetMode=state.mode;
 task(async()=>{let all=[];if(entries.length){for(const entry of entries)all.push(...await readEntry(entry));}else all=files.map(file=>({file,relative:file.name}));if(targetMode==='home')stageHome(all);else if(targetMode==='batch')addFiles(all);else await openRecordings(all);});
});
$('exportAllGraphs').onclick=()=>task(async()=>{
 stashRecord();const jobs=records.flatMap((record,i)=>Object.entries(record.visuals).flatMap(([mode,graphs])=>graphs.filter(Boolean).map((graph,j)=>({graph,name:String(i+1).padStart(2,'0')+'_'+record.file.name.replace(/[^a-zA-Z0-9._-]/g,'_')+'/'+mode+'_plot_'+(j+1)}))));
 if(!jobs.length)throw Error('Choose curves or run analysis in at least one tab first.');
 const format=$('graphFormat').value,images=[];const host=element('div');host.style.cssText='position:fixed;left:-20000px;top:0;width:1400px;height:800px';document.body.append(host);
 try{for(let i=0;i<jobs.length;i++){status('Preparing plot '+(i+1)+' of '+jobs.length+'…');await Plotly.newPlot(host,structuredClone(jobs[i].graph.data),{...structuredClone(jobs[i].graph.layout),width:1400,height:800},{staticPlot:true});const url=await Plotly.toImage(host,{format,width:1400,height:800,scale:2});const blob=await (await fetch(url)).blob();const bytes=new Uint8Array(await blob.arrayBuffer());let binary='';for(let start=0;start<bytes.length;start+=32768)binary+=String.fromCharCode(...bytes.subarray(start,start+32768));images.push({name:jobs[i].name+'.'+format,data:btoa(binary)});Plotly.purge(host);}download(await request({op:'images_zip',images}));status(jobs.length+' plots saved from '+records.length+' open recording tabs in one ZIP.');}finally{Plotly.purge(host);host.remove();}
});
