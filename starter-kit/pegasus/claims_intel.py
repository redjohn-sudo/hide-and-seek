"""Citations proposées, fondées sur citable_claims et les faits locaux du cas."""
import re
from .intel_bridge import Intelligence

class Claims:
    def __init__(self,case,package,intelligence=None):
        self.case=case;self.intel=intelligence or Intelligence(package);self.used=set();self.pending={}

    def restore(self,threads):
        for sid,events in threads.items():
            for m in events:
                if m.get('from')!='buyer':continue
                refs=list(m.get('refs',[]))
                refs += [r for c in m.get('interpreted',{}).get('claims',[]) for r in c.get('refs',[])]
                refs += [r.rstrip('.') for r in re.findall(r'[A-Za-z]+:[A-Za-z0-9_.:@/-]+',m.get('body',''))]
                for ref in refs:
                    if ref.startswith('offer:'):self.used.add((sid,(ref,)))
                    else:self.used.add(ref)

    def pick(self,supplier,part,price,drivers_text='',rivals=()):
        pool=[]
        for oid,value in rivals:
            if not any(oid.startswith('offer:'+s.split(':',1)[1]+'-') for s in self.case['suppliers'] if s!=supplier):continue
            pool.append({'refs':[oid],'kind':'rival_quote','rival':True,
                         'sentence_en':f'We hold a qualified competing offer at EUR {value:.2f} per price unit.'})
        pool.extend(self.intel.catalog(self.case,supplier,price,drivers_text))
        out=[];message_refs=set()
        for c in pool:
            key=(supplier,tuple(c['refs'])) if c.get('rival') or c['kind']=='rival_quote' else None
            if (key and key in self.used) or (not key and set(c['refs']) & self.used):continue
            if len(message_refs | set(c['refs'])) > 20:continue
            message_refs.update(c['refs'])
            out.append({**c,'sentence':c['sentence_en'].rstrip('.')+' ('+', '.join(c['refs'])+').'})
            reserved={key} if key else set(c['refs'])
            self.used.update(reserved);self.pending.setdefault(supplier,set()).update(reserved)
            if len(out)==3:break
        return out

    def commit(self,supplier):self.pending.pop(supplier,None)

    def rollback(self,supplier):self.used.difference_update(self.pending.pop(supplier,set()))
