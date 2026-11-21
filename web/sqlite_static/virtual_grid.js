// The model keeps the full page; only the visible cells are mounted in the DOM.
const virtualGrid={
  active:false,rowHeight:32,columnWidth:160,headerHeight:44,signature:'',columnSignature:'',frame:null,
  plan(columns){
    this.active=!new URLSearchParams(location.search).has('full_grid')&&state.records.length*columns.length>2500;
    const host=el('data-grid'),count=state.records.length,keys=columns.filter(c=>c.primary_key).length;
    let first=0,last=count,start=keys,end=columns.length;
    if(this.active){
      first=Math.max(0,Math.min(count-1,Math.floor((host.scrollTop-this.headerHeight)/this.rowHeight))-6);
      last=Math.min(count,first+Math.ceil(Math.max(host.clientHeight,100)/this.rowHeight)+14);
      start=Math.max(keys,Math.min(columns.length-1,keys+Math.floor(host.scrollLeft/this.columnWidth)-2));
      end=Math.min(columns.length,start+Math.ceil(Math.max(host.clientWidth-36-keys*this.columnWidth,160)/this.columnWidth)+5);
    }
    const segments=[];
    columns.forEach((column,index)=>{if(index<keys||index>=start&&index<end)segments.push({column,index,width:this.columnWidth});else if(!segments.length||!segments[segments.length-1].gap)segments.push({gap:true,width:this.columnWidth});else segments[segments.length-1].width+=this.columnWidth;});
    this.columnSignature=[this.active,start,end,keys,columns.length].join(':');
    this.signature=[this.active,first,last,start,end,host.clientWidth,host.clientHeight].join(':');
    return {first,last,segments,keys,columnSignature:this.columnSignature};
  },
  schedule(){
    if(!this.active||this.frame!==null)return;
    this.frame=requestAnimationFrame(()=>{this.frame=null;const before=this.signature;this.plan(visibleGridColumns());if(before!==this.signature)renderGrid(true);});
  },
  ensure(row,column){
    if(!this.active)return gridCells.get(state.records[row])?.get(visibleGridColumns()[column]?.eng_name);
    const host=el('data-grid'),columns=visibleGridColumns(),keys=columns.filter(c=>c.primary_key).length;
    const top=this.headerHeight+row*this.rowHeight;
    if(top<host.scrollTop+this.headerHeight)host.scrollTop=row*this.rowHeight;
    else if(top+this.rowHeight>host.scrollTop+host.clientHeight)host.scrollTop=top+this.rowHeight-host.clientHeight;
    if(column>=keys){
      const left=36+column*this.columnWidth,frozen=36+keys*this.columnWidth;
      if(left<host.scrollLeft+frozen)host.scrollLeft=left-frozen;
      else if(left+this.columnWidth>host.scrollLeft+host.clientWidth)host.scrollLeft=left+this.columnWidth-host.clientWidth;
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
