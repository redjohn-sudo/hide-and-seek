# hide_and_seek — agent acheteur (Tacto Negotiation Arena)

Le code décide chaque chiffre (coût total C, structures, paliers de concession, plancher relationnel) ;
un gabarit écrit l'e-mail ; un garde-fou vérifie chaque message avant l'envoi. Un worker par fil
fournisseur (`play.py`), les décisions sous un verrou partagé.

## Lancement (depuis `starter-kit/`)

```bash
uv sync --extra sim                                   # ou un venv : pip install -e '.[sim]' pytest
uv run pytest -q tests                                # kit + hide_and_seek, hors ligne
uv run python -m hide_and_seek.dryrun_final                 # répétition hors ligne du chemin des vagues (arène factice)
uv run python -m hide_and_seek.cli practice P04 --parallel 1  # coûte UNE session du quota d'équipe
uv run python -m hide_and_seek.cli final --parallel 10        # Live Final (voir RUNBOOK.md)
```

`practice`, `sandbox` et `final` ouvrent de vraies sessions : quota partagé par l'équipe, à lancer
seulement avec l'accord de l'équipe. `preflight` appelle l'arène et le LLM (réseau).

## Variables d'environnement principales

| Variable | Défaut | Rôle |
|---|---|---|
| `ARENA_URL`, `ARENA_API_KEY` | — | arène (dans `.env`, jamais commité) |
| `ARENA_DATA` | `data/package` | paquet de données (faits citables, correspondance) ; `ARENA_RELEASE` pour la publication du dimanche |
| `HIDE_AND_SEEK_READER` | `0` | `1` : lecture des réponses par LLM (citations vérifiées ligne à ligne), sinon mots-clés |
| `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` | — | LLM du lecteur (API compatible OpenAI) |
| `HIDE_AND_SEEK_READER_BUDGET_S` | `20` | délai httpx d'une lecture LLM |
| `HIDE_AND_SEEK_OPEN_READ_BUDGET_S` | `12` | attente totale des lectures d'ouverture (en parallèle) avant le départ des workers |
| `HIDE_AND_SEEK_READER_CACHE` | `auto` | archive des lectures : `auto`, `replay` (hors ligne), `off` ; fichier `HIDE_AND_SEEK_READER_ARCHIVE` |
| `HIDE_AND_SEEK_TEAM_CLAIMS` | `1` | arguments filtrés par `hollerau/evidence` |
| `HIDE_AND_SEEK_MEMORY` | `data/supplier_memory.json` | mémoire fournisseur entre sessions ; `HIDE_AND_SEEK_PRIOR_MIN` = occurrences avant d'en faire une règle |
| `HIDE_AND_SEEK_ANCHOR`, `HIDE_AND_SEEK_SCHEDULE` | `0.80`, `0,0.22,0.38,…` | ancre et paliers de concession (en part de l'écart en C) |
| `HIDE_AND_SEEK_MESO_TOL`, `HIDE_AND_SEEK_ANCHOR_ALPHA` | `0.04`, `0.5` | fournisseurs stratégiques : tolérance entre paquets, profondeur d'ouverture |
| `HIDE_AND_SEEK_NUDGE`, `HIDE_AND_SEEK_SUBSTANTIATE`, `HIDE_AND_SEEK_TAKE_WORD`, `HIDE_AND_SEEK_CLAUSE_PROBE` | `1` | tactiques actives |
| `HIDE_AND_SEEK_PUSH_AGREED`, `HIDE_AND_SEEK_FINAL_TRADE`, `HIDE_AND_SEEK_RFQ_BAFO`, `HIDE_AND_SEEK_GAP` | `0` | tactiques coupées (réfutées ou non testées) |
| `HIDE_AND_SEEK_FIXED_FIRST`, `HIDE_AND_SEEK_MIRROR_ALONE`, `HIDE_AND_SEEK_CALLOFF`, `HIDE_AND_SEEK_CONTRACT_LEAD`, `HIDE_AND_SEEK_FIRM_JUMP`, `HIDE_AND_SEEK_LATE_TRADE` | `0` | expériences r37-r43, coupées |
| `HIDE_AND_SEEK_CASES` | `analysis/cases-practice.json` | fichiers de cas pour `dryrun_final` |

## Garde-fous

- **Garde-fou de sortie** (`guard.check`, appliqué à la sortie de `decide` pour tout message, quel que soit
  le chemin qui l'a écrit) : chaque ID cité existe (faits du paquet, du cas, offres de la session), chaque
  chiffre à côté d'un ID correspond au fait (tolérance de l'arène), aucune fuite (alternative, son prix,
  budget, urgence) ; une feuille invalide remplace le message par une question sûre.
- **Plancher relationnel** (`_floored`, à la sortie de `decide`) : aucune feuille sous le prix où la charge
  relationnelle dépasse l'économie ; un paquet plus cher pour nous que l'offre en cours est retiré.
  `sync_body` réaligne alors le texte (pas de « attached packages » sans feuille, prix et ligne
  d'engagement du paquet de tête à jour).
- **Clôture garantie** (`play.py`) : un échec de décision, une erreur à l'envoi (422 compris), une resync
  ratée ou la fin anormale du dernier worker lancent le coup de clôture (acceptation de la meilleure
  feuille qui bat l'alternative et l'ouverture, sinon abandon) ; clôture 90 s avant le cap d'une vague.
- **Lecteur** : une citation hors de la ligne citée, une négation ou un chiffre sans son unité est rejeté ;
  aucun appel LLM sous le verrou de décision (mots-clés d'abord, lecture LLM reprise dès qu'elle arrive).
- **Jamais de recul** : les concessions vont dans un seul sens ; un `final_offer` n'est jamais suivi d'un
  contre-paquet chez un fournisseur unique.

## Dépôt

`analysis/sessions/` (534 exports, ~16 Mo) et `analysis/lots/` restent suivis : `.gitignore` n'écarte que
les nouveaux fichiers. Pour les sortir de l'index sans les supprimer du disque :
`git rm -r --cached analysis/sessions analysis/lots` (à décider en équipe : l'analyse hors ligne s'en sert).
