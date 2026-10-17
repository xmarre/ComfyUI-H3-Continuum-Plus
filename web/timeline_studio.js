import { app } from '../../scripts/app.js';
import { api } from '../../scripts/api.js';
import { TimelineEditor } from './timeline/editor.js';
const editors=new Map();
const style=document.createElement('link');style.rel='stylesheet';style.href=new URL('./timeline/studio.css',import.meta.url).href;document.head.append(style);
app.registerExtension({name:'H3Continuum.TimelineStudio',
    beforeRegisterNodeDef(nodeType,nodeData){if(nodeData.name!=='H3ContinuumTimelineStudio')return;
        const created=nodeType.prototype.onNodeCreated,removed=nodeType.prototype.onRemoved,cloned=nodeType.prototype.onClone;
        nodeType.prototype.onNodeCreated=function(){created?.apply(this,arguments);const w=this.widgets.find(x=>x.name==='project_json');if(w){w.hidden=true;w.type='converted-widget';w.computeSize=()=>[0,-4];if(w.inputEl)w.inputEl.style.display='none';}
            this.addWidget('button','Open Timeline Studio',null,()=>{if(editors.has(this)){editors.get(this).root.focus();return;}try{const editor=new TimelineEditor({node:this,widget:w,api,app});editors.set(this,editor);editor.onClose=()=>editors.delete(this);}catch(e){app.extensionManager?.toast?.add({severity:'error',summary:'Timeline Studio',detail:e.message});}});
            this.setSize([330,100]);};
        nodeType.prototype.onRemoved=function(){editors.get(this)?.close();return removed?.apply(this,arguments);};
        nodeType.prototype.onClone=function(){cloned?.apply(this,arguments);const w=this.widgets?.find(x=>x.name==='project_json');if(w){const p=JSON.parse(w.value);p.project_id=crypto.randomUUID().replaceAll('-','');delete p.revision;delete p.accepted;p.takes=[];w.value=JSON.stringify(p);}};
    },
    beforeConfigureGraph(){for(const editor of editors.values())editor.close();editors.clear();},
});
