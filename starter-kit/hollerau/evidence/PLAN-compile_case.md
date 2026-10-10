# Plan : compile_case(case) -> Economics

Nevil, couloir Économie et preuves, 10/10. Branche locale `nevil/evidence`, code dans `hollerau/evidence/`.
Contrat fixé par `hollerau/contracts.py` (non modifié) : `Economics(case_id, cost, must_haves_ok, levers, best_package, c_alt, reference_price)`, avec `Lever(field, best, worst, cost_of_forgetting_eur)`.
Toute l'arithmétique passe par `arena_kit.cost` (`total_cost`, `cost_breakdown`, `must_haves_ok`, `complete_offer`, `closing_terms`). On ne réécrit aucune formule.

## Signature

```python
compile_case(case, standard=None, supplier=None) -> Economics
```

- `standard` : les conditions standard du fournisseur, tirées de son PDF de devis (`arena_kit.docs.standard_terms`). Elles sont inconnues avant la session. Sans elles, `closing_terms` complète avec les termes fixes du cas puis le plan acheteur. Le planificateur **recompile après le tour 0**, quand le devis est lu.
- `supplier` : estimation du fournisseur pour le coût de relation (`full_cost`, `interest_rate`, `standard`). Par défaut, elle est construite par `estimate_supplier()` (voir le cas limite 5).
- Les deux paramètres sont optionnels : l'appel `compile_case(case)` du contrat reste valable.

## Champs

| Champ | Calcul |
|---|---|
| `cost(terms)` | `total_cost(case, terms, standard)` **sans estimation fournisseur** (décision D1) : c'est le C que facture l'arène, vérifié à l'euro près sur la session P01. Infini si la term sheet n'a pas de prix (comportement du kit) |
| `relationship_risk(terms)` (fonction en plus, hors contrat) | coût de relation estimé avec `estimate_supplier()`, en `stabilise` / `secure_supply`. C'est un **avertissement** pour le planificateur, il n'entre pas dans `cost` |
| `must_haves_ok(terms)` | `arena_kit.cost.must_haves_ok(case, terms, standard)` |
| `levers` | pour chaque champ négociable sauf `unit_price` : `best` = la valeur permise qui minimise C, `worst` = celle qui le maximise. On fige le prix à `reference_price` et les autres champs à leur valeur de `best_package`. `cost_of_forgetting_eur` = C(worst) − C(best). Trié du plus coûteux au moins coûteux : c'est le « tip of the spear » |
| `best_package` | les termes hors prix qui minimisent C, sous must-haves, prix à `reference_price`. Recherche exhaustive sur la grille des valeurs permises (au plus 5 × 4 × 4 × 4 = 320 combinaisons sur les 19 cas), sinon descente par coordonnées |
| `c_alt` | `total_cost(case, alternative.terms)` si `alternative.kind != "none"` et qu'il y a un prix, sinon `inf`. Même règle que `closing_move` du kit |
| `reference_price` | `buyer.current_unit_price` (prix unitaire du cas) |

**Valeurs permises d'un champ** : `term_options[field]` uniquement (décision D2 : les 19 cas en ont pour tous leurs champs négociables hors prix). Un champ sans options garde la valeur du plan acheteur, n'est pas traité comme un levier, et il est listé dans `Economics` via les notes de log.

Les valeurs qui cassent un must-have sont **écartées** de la grille.

## Cas limites (chacun a son test)

1. **Must-haves** (P05 `lead_time_days <= 42`, P10 `volume_per_year >= 22230`, P11 `ppm_commitment <= 1500`) : `best_package` et chaque `Lever.best` / `Lever.worst` les respectent. `must_haves_ok(best_package)` est vrai. Si aucune combinaison ne passe, on lève une erreur explicite plutôt que de rendre un paquet invalide.
2. **Alternative absente** (P05, P06, P10, P14, P17 : `kind: none`) : `c_alt == inf`. Alternative `current_contract` (P07, P12, P15) : coûtée comme un devis. Alternative sans `unit_price` : `inf`.
3. **Prix pour 100 ou 1 000** : les 19 dossiers de cas sont en prix unitaires (aucun champ `price_unit`, vérifié). Toute ligne de données comparée à un prix de cas (`po`, `quote`, `cost`) est **divisée par son `price_unit`**. Une fonction `per_unit(row)`, partagée avec `compile_evidence`, s'en charge. Test sur une ligne à `price_unit` 100.
4. **Horizon pluriannuel** (P03, P04, P07, P08, P10, P14 à P16, P18, P19 : 2 ans) : un contrat de 12 mois laisse un an de besoin non couvert au prix de repli + le risque de repli. La règle est celle de `_years` du kit : un engagement au-delà de l'horizon ne coûte ni ne rapporte. Test : sur P03, `contract_months` 24 bat 12, et l'écart égale le besoin non couvert + le risque.
5. **Fournisseur sous sa marge soutenable** (`stabilise` / `secure_supply` : P01 à P03, P06, P09, P10, P14, P17) :
   - `estimate_supplier()` prend `full_cost` = `should_cost` par unité (`cost:<part>`) s'il existe.
   - Sinon, prix courant × (1 − marge soutenable du cas), qui suppose qu'il est à l'équilibre et le marque `estimated`.
   - `interest_rate` vient de `buyer.interest_rate` par défaut. On le relève si un signal cash existe : c'est le couloir planificateur, pas ici.
   - Mesuré sur la session P01 : avec le should-cost comme coût complet, le deal à 135,40 € coûterait 4 055 € de relation, **alors que l'arène facture 0**. D'où la décision D1 : `cost` n'inclut pas la relation, et `relationship_risk` l'expose à part.
   - Test P01 : `relationship_risk` > 0 au prix courant (126,53 €) et `cost` = C de l'arène.
6. **Prix absent** d'une term sheet : `cost` renvoie `inf` (kit), et `levers` le gère sans planter.

## Tests (`hollerau/evidence/tests/test_compile_case.py`)

- Sur les 19 cas : `compile_case` ne lève pas d'erreur, `must_haves_ok(best_package)`, `cost(best_package) <= cost(référence)`, `levers` triés, chaque `best` et `worst` dans la grille et compatible avec les must-haves.
- Cas limites 1 à 6 ci-dessus.
- Cohérence avec le kit : `cost(t) == arena_kit.cost.total_cost(case, t, standard, supplier)` pour quelques t.
- **Contrôle croisé avec la session réelle** (`tests/fixtures/sessions/P01-naive.json`) : C du deal enregistré recalculé avec les termes du deal et les conditions standard du devis = `c_deal` de la trace, à 0,01 € près. C'est la preuve qu'on compte comme l'arène.

**Conditions standard du devis** : avant le tour 0, elles sont inconnues. Sur P01, le standard ppm de 900 vaut 1 584 € de coût qualité qui manque avant la lecture du devis. Test : `compile_case(case, standard)` ≠ `compile_case(case)` sur le poste qualité, et la recompilation après le tour 0 est obligatoire dans le planificateur.

## Hors périmètre

- Choisir le prix à demander : c'est le planificateur (`plan`).
- Estimer le taux de financement ou le plancher du fournisseur pendant la partie : c'est `Belief`.
- Lecture du PDF de devis : `arena_kit.docs.standard_terms` existe, on l'appelle.

## Revue ingénierie (/plan-eng-review, 10/10)

Cible : ce plan. Preuves : `arena_kit/cost.py`, `tests/fixtures/cases/practice.json` (19 cas), `tests/fixtures/sessions/P01-naive.json`. Scope : accepté tel quel, simplifié par D2.

### Constats et décisions

- [P1] (confidence: 9/10) plan, cas limite 5 : avec `full_cost` = should-cost, le coût de relation diverge de l'arène (4 055 € contre 0 € sur le deal P01 à 135,40 €, mesuré). **D1 → A** : `cost` sans relation, `relationship_risk` à part.
- [P2] (confidence: 9/10) plan, grille dérivée : les 19 cas ont des `term_options` pour tous les champs négociables hors prix (sondage). **D2 → A** : options seulement.
- [P2] (confidence: 9/10) conditions standard inconnues avant le devis : 1 584 € de coût qualité manquant sur P01 (ppm standard de 900). Preuve requise ajoutée : test avec et sans `standard`, recompilation au tour 0.
- [Vérifié] (confidence: 10/10) contrôle croisé : `total_cost` du kit sur les termes du deal P01 donne `c_deal` = 1 120 579,36 € et `c_alt` = 1 176 178,02 €, identiques à l'arène.
- Performance : grille de 75 combinaisons au plus, donc recherche exhaustive en millisecondes, sans souci.

### Tests à écrire (`hollerau/evidence/tests/test_compile_case.py`)

```
compile_case
  ├── 19 cas : pas d'erreur, must_haves_ok(best_package), cost(best_package) <= cost(référence)   [unit]
  ├── levers triés, best/worst dans term_options et compatibles must-haves                          [unit]
  ├── must-haves P05 / P10 / P11 respectés ; aucune combinaison valide → erreur explicite             [unit]
  ├── c_alt : inf pour kind none (P05, P06, P10, P14, P17) ; current_contract coûté (P07)             [unit]
  ├── horizon 2 ans (P03) : 24 mois bat 12 mois de (non couvert + risque)                             [unit]
  ├── per_unit() : ligne de données à price_unit 100 divisée                                          [unit]
  ├── relationship_risk(P01, prix courant) > 0 ; cost n'en tient pas compte                           [unit]
  ├── standard du devis : le poste qualité change (P01, ppm 900)                                      [unit]
  └── session P01 : cost(terms du deal) == c_deal arène à 0,01 € près ; c_alt idem                    [contrat]
```

### Modes d'échec

| Chemin | Échec | Gestion |
|---|---|---|
| cas caché sans `term_options` sur un champ | pas de levier pour ce champ | valeur du plan acheteur, noté, pas d'invention |
| must-haves incompatibles avec toutes les options | aucun paquet valide | erreur explicite, le planificateur ne propose rien de faux |
| devis non lu | C sous-estimé (qualité, paiement standard) | recompilation obligatoire au tour 0 |

Gap critique : aucun.

### NOT in scope

Choix du prix (planificateur), estimation en cours de partie du coût et du taux du fournisseur (`Belief`), calibration de `full_cost` sur les replays (option C de D1, à reprendre quand on aura des sessions validées par Icham).

### Completion summary

- Step 0 : Scope Challenge — scope accepted as-is (grille simplifiée par D2)
- Architecture Review : 1 issue found (relation, D1)
- Code Quality Review : 1 issue found (grille inutile, D2)
- Test Review : diagramme produit, 9 gaps identifiés, couverts le 10/10 soir par `tests/test_compile_case.py`
- Performance Review : 0 issues found
- Failure modes : 0 critical gaps flagged
- Unresolved decisions : 0
- Outside voice : codex unavailable (CLI trop ancien), pas de repli natif
- Parallelization : sequential implementation, no parallelization opportunity

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Outside Review | codex (`/plan-eng-review` outside voice) | Independent 2nd opinion | 1 | unavailable | CLI Codex trop ancien |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | ISSUES OPEN | 11 issues (dont 9 tests à écrire), 0 critical gaps |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | — |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

- **OUTSIDE COVERAGE:** codex, plan-review, unavailable, 0 constat.
- **VERDICT:** décisions D1 et D2 prises, aucun point ouvert : prêt à implémenter.

NO UNRESOLVED DECISIONS
