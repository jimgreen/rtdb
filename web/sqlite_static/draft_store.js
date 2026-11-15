// IndexedDB transactions keep large draft writes off synchronous Web Storage.
const draftStore={
  cache:new Map(),queue:Promise.resolve(),database:null,
  async init(){
    this.database=await new Promise((resolve,reject)=>{
      const request=indexedDB.open('rtdb-local-drafts',1);
      request.onupgradeneeded=()=>request.result.createObjectStore('drafts',{keyPath:'key'});
      request.onsuccess=()=>resolve(request.result);request.onerror=()=>reject(request.error);
    });
    await this.refresh();
    for(const key of Object.keys(localStorage)){
      if(!key.startsWith('rtdb-cell-draft-v1:')&&!key.startsWith('rtdb-draft-emergency:'))continue;
      try{
        const value=JSON.parse(localStorage.getItem(key));
        if(key.startsWith('rtdb-cell-draft-v1:'))Object.assign(value,{key,scope:JSON.parse(key.slice('rtdb-cell-draft-v1:'.length))[0],kind:'data',owner:'legacy'});
        if(!this.cache.has(value.key)||value.at>this.cache.get(value.key).at)await this.put(value);
        localStorage.removeItem(key);
      }catch(error){notice('旧草稿迁移未完成，原草稿仍保留：'+error.message,true);}
    }
  },
  async refresh(){
    const values=await new Promise((resolve,reject)=>{
      const request=this.database.transaction('drafts').objectStore('drafts').getAll();
      request.onsuccess=()=>resolve(request.result);request.onerror=()=>reject(request.error);
    });
    this.cache=new Map(values.map(value=>[value.key,value]));
  },
  mutate(action){
    const job=this.queue.catch(()=>{}).then(()=>new Promise((resolve,reject)=>{
      const tx=this.database.transaction('drafts','readwrite');action(tx.objectStore('drafts'));
      tx.oncomplete=resolve;tx.onerror=()=>reject(tx.error);tx.onabort=()=>reject(tx.error||Error('草稿事务中断'));
    }));this.queue=job;return job;
  },
  put(value){this.cache.set(value.key,value);return this.mutate(store=>store.put(value));},
  async remove(key,revision){
    let removed=false;
    await this.mutate(store=>{const request=store.get(key);request.onsuccess=()=>{if(request.result&&(!revision||request.result.revision===revision)){store.delete(key);removed=true;}};});
    if(removed&&(!revision||this.cache.get(key)?.revision===revision))this.cache.delete(key);
  }
};
