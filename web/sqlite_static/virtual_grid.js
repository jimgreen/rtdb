// 仅挂载视口内可见行列；按真实列宽前缀和精确对齐拖拽后的列宽。
const virtualGrid={
  active:false,rowHeight:32,columnWidth:160,headerHeight:44,signature:'',columnSignature:'',frame:null,
  columnWidths(columns){
    const saved = typeof loadColumnWidths === 'function' ? loadColumnWidths() : {};
    const tableName = typeof state !== 'undefined' ? (state.table || '') : '';
    const widths = new Array(columns.length);
    const offsets = new Array(columns.length + 1);
    offsets[0] = 0;
    for (let i = 0; i < columns.length; i++) {
      const col = columns[i];
      const key = `${tableName}::${col.eng_name}`;
      const w = Number(saved[key]) || this.columnWidth;
      widths[i] = w;
      offsets[i + 1] = offsets[i] + w;
    }
    return { widths, offsets };
  },
  plan(columns){
    this.active=!new URLSearchParams(location.search).has('full_grid')&&state.records.length*columns.length>2500;
    const host=el('data-grid'),count=state.records.length,keys=columns.filter(c=>c.primary_key).length;
    const { widths, offsets } = this.columnWidths(columns);
    let first=0,last=count,start=keys,end=columns.length;
    if(this.active){
      first=Math.max(0,Math.min(count-1,Math.floor((host.scrollTop-this.headerHeight)/this.rowHeight))-6);
      last=Math.min(count,first+Math.ceil(Math.max(host.clientHeight,100)/this.rowHeight)+14);
      const frozenW = 36 + offsets[keys];
      const viewLeft = host.scrollLeft + offsets[keys];
      const viewRight = viewLeft + Math.max(host.clientWidth - frozenW, 160);
      let s = keys;
      while (s < columns.length - 1 && offsets[s + 1] < viewLeft) s++;
      start = Math.max(keys, s - 2);
      let e = start;
      while (e < columns.length && offsets[e] < viewRight) e++;
      end = Math.min(columns.length, e + 3);
    }
    const segments=[];
    columns.forEach((column,index)=>{
      const w = widths[index];
      if(index<keys||index>=start&&index<end)segments.push({column,index,width:w});
      else if(!segments.length||!segments[segments.length-1].gap)segments.push({gap:true,width:w});
      else segments[segments.length-1].width+=w;
    });
    this.columnSignature=[this.active,start,end,keys,columns.length].join(':');
    this.signature=[this.active,first,last,start,end,host.clientWidth,host.clientHeight,offsets[columns.length]].join(':');
    return {first,last,segments,keys,totalWidth:offsets[columns.length],keyOffsets:offsets.slice(0,keys),columnSignature:this.columnSignature};
  },
  schedule(){
    if(!this.active||this.frame!==null)return;
    this.frame=requestAnimationFrame(()=>{this.frame=null;const before=this.signature;this.plan(visibleGridColumns());if(before!==this.signature)renderGrid(true);});
  },
  ensure(row,column){
    if(!this.active)return gridCells.get(state.records[row])?.get(visibleGridColumns()[column]?.eng_name);
    const host=el('data-grid'),columns=visibleGridColumns(),keys=columns.filter(c=>c.primary_key).length;
    const { widths, offsets } = this.columnWidths(columns);
    const top=this.headerHeight+row*this.rowHeight;
    if(top<host.scrollTop+this.headerHeight)host.scrollTop=row*this.rowHeight;
    else if(top+this.rowHeight>host.scrollTop+host.clientHeight)host.scrollTop=top+this.rowHeight-host.clientHeight;
    if(column>=keys){
      const left=36+offsets[column],frozen=36+offsets[keys],colW=widths[column]||this.columnWidth;
      if(left<host.scrollLeft+frozen)host.scrollLeft=Math.max(0,left-frozen);
      else if(left+colW>host.scrollLeft+host.clientWidth)host.scrollLeft=left+colW-host.clientWidth;
    }
    const before=this.signature;this.plan(columns);if(before!==this.signature)renderGrid(true);
    return gridCells.get(state.records[row])?.get(columns[column]?.eng_name);
  },
  init(){
    el('data-grid').addEventListener('scroll',()=>this.schedule(),{passive:true});
    this.observer=new ResizeObserver(()=>this.schedule());this.observer.observe(el('data-grid'));
    document.addEventListener('pointermove',event=>{this.pointer=event;if(state.cellDrag&&!this.dragFrame)this.dragFrame=requestAnimationFrame(()=>this.drag());});
  },
  drag(){
    this.dragFrame=null;if(!state.cellDrag)return;
    const host=el('data-grid'),box=host.getBoundingClientRect(),event=this.pointer;
    const dx=event.clientX>box.right-20?20:event.clientX<box.left+20?-20:0;
    const dy=event.clientY>box.bottom-20?20:event.clientY<box.top+this.headerHeight+15?-20:0;
    if(!dx&&!dy)return;
    const x=host.scrollLeft,y=host.scrollTop;host.scrollLeft+=dx;host.scrollTop+=dy;
    if(x===host.scrollLeft&&y===host.scrollTop)return;
    if(this.active){const previous=this.signature;this.plan(visibleGridColumns());if(previous!==this.signature)renderGrid(true);}
    updateCellSelectionFromPointer({pointerId:event.pointerId,clientX:Math.max(box.left+40,Math.min(box.right-5,event.clientX)),clientY:Math.max(box.top+this.headerHeight+5,Math.min(box.bottom-5,event.clientY))});
    this.dragFrame=requestAnimationFrame(()=>this.drag());
  }
};
