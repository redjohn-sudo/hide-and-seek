"""Pont proposé vers intel : stdlib, aucune connexion ; cibles réservées à practice."""
import copy
import json
import math
import re
from functools import lru_cache
from pathlib import Path

from intel import Package, agent_brief
from intel.dossier import month_shift

@lru_cache(maxsize=4)
def _load_package(root, release, fingerprint):
    # Immutable après lecture ; partage des tables entre les agents d'une vague.
    return Package(root, release)


class Intelligence:
    def __init__(self, package=None, playbook=None):
        self.package=package
        self.book=playbook or {}
        self.root=Path(package.root) if package and getattr(package,'root',None) else None
        self._pkg=None
        self.diagnostics=[];self._base_facts=None
        self._dossiers={};self._briefs={}

    def pkg(self):
        if self._pkg is None and self.root:
            release = Package.find_release(self.root, getattr(self.package, 'release', None))
            paths = []
            for base in [self.root] + ([release] if release else []):
                paths.extend(p for p in base.rglob('*') if p.is_file()
                             and p.suffix in {'.json', '.sqlite', '.csv', '.md'})
            fingerprint = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in sorted(paths))
            self._pkg = _load_package(str(self.root), str(release) if release else None, fingerprint)
        return self._pkg

    def brief(self,case,sid):
        """Dossier fournisseur dans tous les sets, y compris une nouvelle combinaison.

        Les signaux sont des hypothèses sourcées et ne sont pas des claims API.
        Aucun fair-price d'une autre pièce n'est transporté vers le cas courant.
        """
        key = (case['case_id'], sid, tuple(case['parts']))
        if key not in self._briefs:
            pkg = self.pkg()
            if pkg is None or sid not in pkg.ids:
                self._briefs[key] = {'supplier_id':sid, 'signals':[], 'citable_claims':[],
                                     'status':'supplier_absent_from_package'}
            else:
                if sid not in self._dossiers:
                    self._dossiers[sid] = pkg.dossier(sid)
                dossier = self._dossiers[sid]
                brief = agent_brief(dossier, case=case)
                brief['signals'] = copy.deepcopy(dossier['signals'])
                for field in ('fair_price', 'benchmark_quotes'):
                    brief[field] = {pid:value for pid,value in brief[field].items() if pid in case['parts']}
                brief['citable_claims'] = [claim for claim in brief['citable_claims']
                    if all(pkg.facts.get(ref, {}).get('part') in [None] + case['parts']
                           for ref in claim['refs'])]
                brief['requested_supplier_id'] = sid
                brief['status'] = 'package_supplier'
                self._briefs[key] = brief
        return self._briefs[key]

    def same_supplier(self,a,b):
        aliases = self.pkg().aliases if self.pkg() else {}
        return aliases.get(a,a) == aliases.get(b,b)

    def target(self,case):
        if case.get('set')!='practice':return None
        if 'cases' in self.book:
            entry=self.book['cases'].get(case['case_id'],{})
            target = entry.get('cost_budget_for_score_99')
            return target if isinstance(target, (int,float)) and math.isfinite(target) else None
        target = (self.book.get(case['case_id']) or {}).get('target_c')
        return target if isinstance(target, (int,float)) and math.isfinite(target) else None

    def estimate(self,case,sid,std):
        b=case['buyer']
        if b.get('category_goal') not in {'stabilise','secure_supply'} or not b.get('relationship_eur_per_point') or not b.get('sustainable_margin_pct'):
            return None,'inactive'
        if case.get('set')=='practice':
            entry=self.book.get('cases',{}).get(case['case_id'],{}).get('suppliers',{}).get(sid,{})
            model=entry.get('supplier_model',{}).get('supplier')
            if model:
                model=copy.deepcopy(model);model['standard']=dict(std)
                return model,'practice_estimate_calibrated_ex_post'
        cost=None
        pkg=self.pkg()
        if pkg and len(case['parts'])==1:
            try:
                p=self.brief(case,sid)['fair_price'][case['parts'][0]]
                c=p.get('should_cost')
                unit=pkg.ids[case['parts'][0]][1].get('price_unit',1)
                buyer_unit=b.get('price_unit',unit)
                if c and c['applicable_without_country_adjustment'] and c['price_unit']['value']==unit==buyer_unit:
                    cost=c['estimate']['value']
            except (ValueError,KeyError):pass
        if cost is None and len(case['parts']) == 1:
            for ref,f in (case.get('facts') or {}).items():
                if ref.startswith('cost:') and f.get('part') in case['parts'] and f.get('supplier') in {None,sid} and f.get('unit_cost',0)>0 and f.get('price_unit',1)==b.get('price_unit',1):
                    row = pkg.ids.get(ref, (None, {}))[1] if pkg else {}
                    supplier_row = pkg.ids.get(sid, (None, {}))[1] if pkg else {}
                    if row.get('cost_base') and row.get('supplier_id') != sid and row['cost_base'] != supplier_row.get('country'):
                        continue
                    cost=f['unit_cost'];break
        if cost is None:
            self.diagnostics.append({'supplier_id':sid,'kind':'relationship_unknown','meaning':'C sans relation est une borne optimiste, pas une estimation complète'})
            return None,'unknown_optimistic_lower_bound'
        return {'standard':dict(std),'full_cost':cost,'interest_rate':.08,'usual_rate':.08,
                'ppm':std.get('ppm_commitment',0),'extra':0},'should_cost_cash_rate_hypothesis_8pct'

    def facts(self,case):
        if case.get('set')=='practice' and self.root:
            if self._base_facts is None:self._base_facts=json.loads((self.root/'facts.json').read_text())
            pool=self._base_facts
        else:
            pool=dict(self.pkg().facts if self.pkg() else getattr(self.package,'facts',{}) or {})
        return {**pool,**(case.get('facts') or {})}

    def catalog(self,case,sid,price,drivers_text=''):
        facts=self.facts(case);pool=[]
        brief = self.brief(case,sid)
        pool.extend(brief['citable_claims'])
        # Discover case-only and release IDs rather than rely on pkg.ids alone.
        def add(kind,refs,value,text):
            pool.append({'kind':kind,'refs':refs,'asserted_value':value,'sentence_en':text})
        for pid in case['parts']:
            forecasts=sorted((ref,f) for ref,f in facts.items() if ref.startswith('fc:') and f.get('part')==pid)
            if forecasts:
                ref,f=forecasts[-1];v=f.get('change_pct',0)
                if v>.5:add('volume_change',[ref],v,f'The recorded demand forecast for {pid} increased by {v:g}%.')
            for ref,f in facts.items():
                if ref.startswith('cost:') and f.get('part')==pid and f.get('supplier') in {None,sid} and f.get('unit_cost',0)>0:
                    # Do not introduce a raw estimate with mismatched price units.
                    unit=1
                    if self.pkg() and pid in self.pkg().ids:unit=self.pkg().ids[pid][1].get('price_unit',1)
                    country_ok=True
                    if self.pkg():
                        sr=self.pkg().ids.get(sid,(None,{}))[1]
                        row=self.pkg().ids.get(ref,(None,{}))[1]
                        if row.get('cost_base'):
                            country_ok=row.get('supplier_id')==sid or row['cost_base']==sr.get('country')
                        else:country_ok=f.get('country') in {None,sr.get('country')}
                    if country_ok and f.get('price_unit',1)==unit and price and f['unit_cost']<price:
                        add('should_cost',[ref],f['unit_cost'],f'The should-cost estimate for {pid} is EUR {f["unit_cost"]:g} per price unit.')
        cards=sorted((ref,f) for ref,f in facts.items() if ref.startswith('sc:') and self.same_supplier(f.get('supplier'),sid))
        if cards:
            ref,f=cards[-1]
            if f.get('ppm') is not None and (f['ppm']>1000 or case['buyer'].get('category_goal')=='quality_first'):
                add('quality_record',[ref],f['ppm'],f'Your recorded quality is {f["ppm"]:g} ppm.')
            if f.get('otd_pct') is not None and f['otd_pct']<90:
                add('delivery_record',[ref],f['otd_pct'],f'Your recorded on-time delivery is {f["otd_pct"]:g}%.')
        letters=[(r,f) for r,f in facts.items() if r in case.get('refs',[]) and r.startswith('doc:') and self.same_supplier(f.get('supplier'),sid) and f.get('days') is not None]
        contracts=[(r,f) for r,f in facts.items() if r in case.get('refs',[]) and r.startswith('ctr:') and self.same_supplier(f.get('supplier'),sid) and f.get('days') is not None]
        for a,fa in letters:
            for b,fb in contracts:
                if fa['days']<fb['days']:
                    add('notice_period',[a,b],fa['days'],f'The notice letter gives {fa["days"]} days of notice against the referenced contract.')
        pkg=self.pkg()
        cutoff=pkg.as_of[:7] if pkg else '2026-09'
        cutoff=max([cutoff]+[f['date'][:7] for f in (case.get('facts') or {}).values() if f.get('date')])
        relevant={r['series_id'] for pid in case['parts'] for r in (pkg.rows('cost_drivers','part_id',pid) if pkg else [])}
        relevant.update(re.findall(r'\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+\b',drivers_text or ''))
        for series in relevant:
            points=sorted((f.get('month'),r,f) for r,f in facts.items() if r.startswith('idx:') and f.get('series')==series and f.get('month') and f['month']<=cutoff)
            if len(points)<2:continue
            end,br,bf=points[-1]
            bases=[p for p in points[:-1] if p[0]>=month_shift(end,-12) and p[2].get('value',0)>0]
            if end<month_shift(cutoff,-12) or not bases:continue
            _,ar,af=bases[0];v=(bf['value']/af['value']-1)*100
            if v<-.5:add('index_change',[ar,br],v,f'{series} changed by {v:.4f}% over the referenced window.')
        unique={}
        for c in pool:
            if not all(r in facts for r in c['refs']):continue
            kind=c['kind'];fs=[facts[r] for r in c['refs']];v=c['asserted_value']
            if kind not in {'notice_period','quality_record','delivery_record','volume_change','should_cost','index_change','order_history','rival_quote'}:continue
            if any(f.get('part') is not None and f['part'] not in case['parts'] for f in fs):continue
            if kind in {'quality_record','delivery_record'} and any(not self.same_supplier(f.get('supplier'),sid) for f in fs):continue
            if kind=='index_change':
                a,b=fs
                if a.get('series') not in relevant or a.get('series')!=b.get('series') or b.get('month','')<month_shift(cutoff,-12) or b.get('month','')>cutoff:continue
                if v>=-.5 or a.get('month','')<month_shift(b.get('month','2026-09'),-12):continue
            if kind=='notice_period' and (c['refs'][0] not in case.get('refs',[]) or v!=fs[0].get('days')):continue
            if kind=='notice_period' and (not self.same_supplier(fs[0].get('supplier'),sid) or not self.same_supplier(fs[1].get('supplier'),sid) or fs[0]['days']>=fs[1]['days']):continue
            if kind=='should_cost':
                if not price or v>=price:continue
                pid=fs[0].get('part');unit=1
                if self.pkg() and pid in self.pkg().ids:unit=self.pkg().ids[pid][1].get('price_unit',1)
                if fs[0].get('price_unit',1)!=unit or fs[0].get('price_unit',1)!=case['buyer'].get('price_unit',unit):continue
                if self.pkg():
                    row=self.pkg().ids.get(c['refs'][0],(None,{}))[1]
                    sr=self.pkg().ids.get(sid,(None,{}))[1]
                    if row.get('cost_base') and row.get('supplier_id')!=sid and row['cost_base']!=sr.get('country'):continue
            if kind=='volume_change' and v<=.5:continue
            if kind=='rival_quote':
                allowed = set(case.get('refs',[]))
                alt = case['buyer'].get('alternative') or {}
                if alt.get('ref'):allowed.add(alt['ref'])
                if c['refs'][0] not in allowed or self.same_supplier(fs[0].get('supplier'),sid) or not price or v>=price:continue
            # La valeur provenant du dossier doit encore correspondre aux faits
            # autoritaires du cas courant (les faits privés ont priorité).
            field = {'quality_record':'ppm', 'delivery_record':'otd_pct',
                     'volume_change':'change_pct', 'should_cost':'unit_cost',
                     'rival_quote':'unit_price'}.get(kind)
            if field and v != fs[0].get(field):continue
            if kind=='order_history' and any(f.get('unit_price')!=v or not self.same_supplier(f.get('supplier'),sid) for f in fs):continue
            if kind=='index_change':
                if not fs[0].get('value'):continue
                actual = (fs[1]['value']/fs[0]['value']-1)*100
                if not math.isclose(v,actual,rel_tol=1e-9,abs_tol=1e-8):continue
            priority={'notice_period':1,'volume_change':3,'should_cost':4,'index_change':5,'order_history':7,'rival_quote':8,
                      'quality_record':0 if case['buyer'].get('category_goal')=='quality_first' else 6,'delivery_record':6}
            # Achats couverts : l'indice reste vérifiable, mais ne prouve pas une baisse immédiate.
            if kind == 'index_change' and any(v['type']=='hedged_inputs' for v in brief.get('signals',[])):
                priority[kind] = 9
            unique.setdefault((kind,tuple(c['refs'])),{**c,'priority':priority[kind]})
        return sorted(unique.values(),key=lambda c:(c['priority'],c['refs']))
