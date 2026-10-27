# Finale — mode d'emploi (dimanche)

Tout se lance depuis `~/tacto/negotiation-arena-kit/starter-kit` sur une machine sur secteur, sans mise en veille, de préférence sur un réseau fiable (pas le Wi-Fi de la salle si possible). Les clés sont dans `.env` (jamais commitées).

| Heure | Commande | Attendu |
|---|---|---|
| 08:05 | `uv run arena data` | `data/package/release-sunday/` apparaît (nouvelles news, lettres, mémo) |
| 08:10 | `HIDE_AND_SEEK_READER=1 uv run python -m hide_and_seek.preflight` | 11/11 verts (dont « sunday release unpacked ») |
| 09:00 | `HIDE_AND_SEEK_READER=1 HIDE_AND_SEEK_TEAM_CLAIMS=1 uv run python -m hide_and_seek.cli practice P04 --parallel 1` | une partie complète passe par la même machine, la même clé et le même réseau que la finale (coûte 1 session) |
| 09:30 | `uv run python -m hide_and_seek.dryrun_final` | répétition hors ligne du chemin des vagues (arène factice) : 2 OK |
| 11:40 | `rm -f data/reader_archive.json.tmp; ls data/supplier_memory.json` | la mémoire fournisseur des entraînements reste (fournisseurs de la finale = jeu de données) |
| **11:45** | `HIDE_AND_SEEK_READER=1 HIDE_AND_SEEK_TEAM_CLAIMS=1 nohup uv run python -m hide_and_seek.cli final --parallel 10 > final.log 2>&1 &` | le runner attend la vague 1 (`403` tant qu'elle n'est pas ouverte), joue chaque vague en parallèle, clôt 90 s avant le cap, s'arrête après la 5e vague |
| 12:00 | `tail -f final.log` | une ligne par coup ; **ne jamais** ouvrir `/leaderboard`, `/results`, `/screen`, `/v1/events` depuis l'agent (règle de la finale) |

## Si ça casse
- Le runner plante : relancer la même commande ; il reprend les sessions ouvertes (`409 … one session per case` → reprise automatique).
- Le LLM du lecteur ne répond plus : l'agent continue avec le repli par mots-clés (aucun arrêt). Pour couper le LLM : `HIDE_AND_SEEK_READER=0`.
- L'adaptateur de Nevil absent (`hollerau/` manquant) : repli automatique sur nos arguments.
- Quota : la finale ne compte pas dans les 450 sessions.

## Réglages en vigueur (défauts, r15)
Ancre 80 % du prix ; cas relationnels : modèle calibré sur les pénalités réelles de l'arène (coût complet = should-cost/1,02 ; coût de l'argent 9,6 % si la correspondance montre un fournisseur à court de trésorerie, 6,4 % sinon, puis affiné par ses propres paires de paiement), ouverture sur la structure au creux le plus bas, chaque paquet plafonné au coût de son offre courante et à 4 % de l'économie du meilleur paquet (`HIDE_AND_SEEK_MESO_TOL`), clause d'indexation voulue offerte une fois à son creux. Concessions en C `0,22/0,38/0,52/0,63/…`, jamais de recul. Acceptation : `agreed` pris tout de suite (`HIDE_AND_SEEK_PUSH_AGREED=0`), `final_offer` acceptée dès que C < C_open (+0,5 % si l'alternative est pire, jamais de contre après), avant d'accepter une offre `open` un seul « nudge » d'un tick sous son prix (`HIDE_AND_SEEK_NUDGE=1`), réponse aux questions chiffrées du bot avec la prévision + CSV joint (`HIDE_AND_SEEK_SUBSTANTIATE=1`). Clôture 90 s avant le cap avec notre coût (pénalité relationnelle comprise). Autres variables : `HIDE_AND_SEEK_ANCHOR`, `HIDE_AND_SEEK_SCHEDULE`, `HIDE_AND_SEEK_GAP`, `HIDE_AND_SEEK_FINAL_TRADE`, `HIDE_AND_SEEK_RFQ_BAFO`, `HIDE_AND_SEEK_PRIOR_MIN`.

## Soumission ehl.gg (avant 12:00)
Dépôt `IchamRaison/hollerau-agent` (PR #8 = ce code), hash du commit lancé, description de l'approche (`hollerau/hide_and_seek/README.md`), slides.
