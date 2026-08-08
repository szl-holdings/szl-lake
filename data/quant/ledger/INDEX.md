# szl-quant receipt ledger

Append-only DSSE receipt ledger written by the `scheduled-paper` workflow.
Every file is an advisory **paper-only** decision receipt, ed25519-signed by the
engine identity pinned at `keys/engine_pubkey.json` on `main`.

Verify any entry independently:
```
node verify/verify.mjs --pubkey keys/engine_pubkey.json --dir ledger/<run-dir>/
```

Runs recorded: 107 · receipts: 1153 (MEASURED from files present; cron is best-effort, gaps are honest)

Track record (latest, MEASURED from verified receipts only): +1d n=0 hit=— pending=0 · +7d n=0 hit=— pending=0 · no-calls(BLOCKED)=358 — a past frequency, NOT a prediction

Hash chain: 105 link(s), head seq 105 sha256 d4c4976fd560… — every sealed run tamper-evident; walk it: `node verify/verify.mjs --pubkey keys/engine_pubkey.json --chain ledger/`

Paper book (MODELED, paper-only — NOT real funds): seq 99 · equity $10000.000000 · open positions 0 · fills this run 0 · replay it: `node verify/verify.mjs --pubkey keys/engine_pubkey.json --book ledger/`

Refusal record (MEASURED): latest run BLOCKED 3/6 — liquidity×3 · lifetime (recorded runs) 323/588 — liquidity×209 conviction×125 · a refusal is a decision, not an absence · replay: `node verify/verify.mjs --pubkey keys/engine_pubkey.json --refusals ledger/`

External witness (REPORTED, SET + Merkle inclusion + log consistency offline-verifiable): chain head seq 105 anchored in Rekor — logIndex 2386630928, uuid 108e9186e8c5677a… · heads anchored 105/105 · inclusion proven offline 105/105 · log consistency 104/104 adjacent checkpoint pair(s) receipted · second witness (RFC 3161) 105/105 head(s) countersigned · cross-witness gossip 92 observation(s) from a second scheduled observer · an anchored head cannot be silently truncated · check: `node verify/verify.mjs --pubkey keys/engine_pubkey.json --witness .`

| run (UTC) | receipts |
|---|---|
| 20260715T090057Z_run1 | 5 |
| 20260715T092114Z_run2 | 6 |
| 20260715T094346Z_run3 | 9 |
| 20260715T141549Z_run4 | 9 |
| 20260715T194057Z_run5 | 9 |
| 20260716T034408Z_run6 | 9 |
| 20260716T083822Z_run7 | 9 |
| 20260716T142836Z_run8 | 9 |
| 20260716T173736Z_run9 | 10 |
| 20260716T175421Z_run10 | 11 |
| 20260716T183506Z_run12 | 11 |
| 20260716T185648Z_run13 | 11 |
| 20260716T193525Z_run14 | 11 |
| 20260716T193839Z_run15 | 11 |
| 20260716T202015Z_run16 | 11 |
| 20260716T205840Z_run17 | 11 |
| 20260717T034344Z_run18 | 11 |
| 20260717T083225Z_run19 | 11 |
| 20260717T140759Z_run20 | 11 |
| 20260717T193725Z_run21 | 11 |
| 20260718T033902Z_run22 | 11 |
| 20260718T081319Z_run23 | 11 |
| 20260718T135458Z_run24 | 11 |
| 20260718T193657Z_run25 | 11 |
| 20260719T040053Z_run26 | 11 |
| 20260719T083827Z_run27 | 11 |
| 20260719T135816Z_run28 | 11 |
| 20260719T193725Z_run29 | 11 |
| 20260720T041006Z_run30 | 11 |
| 20260720T093941Z_run31 | 11 |
| 20260720T143704Z_run32 | 11 |
| 20260720T200051Z_run33 | 11 |
| 20260721T035212Z_run34 | 11 |
| 20260721T090551Z_run35 | 11 |
| 20260721T142851Z_run36 | 11 |
| 20260721T195703Z_run37 | 11 |
| 20260722T035303Z_run38 | 11 |
| 20260722T084929Z_run39 | 11 |
| 20260722T142957Z_run40 | 11 |
| 20260722T194946Z_run41 | 11 |
| 20260723T035028Z_run42 | 11 |
| 20260723T084850Z_run43 | 11 |
| 20260723T143822Z_run44 | 11 |
| 20260723T195058Z_run45 | 11 |
| 20260724T035011Z_run46 | 11 |
| 20260724T084718Z_run47 | 11 |
| 20260724T141802Z_run48 | 11 |
| 20260724T195142Z_run49 | 11 |
| 20260725T034430Z_run50 | 11 |
| 20260725T082808Z_run51 | 11 |
| 20260725T140652Z_run52 | 11 |
| 20260725T193910Z_run53 | 11 |
| 20260726T040329Z_run54 | 11 |
| 20260726T084253Z_run55 | 11 |
| 20260726T140323Z_run56 | 11 |
| 20260726T194030Z_run57 | 11 |
| 20260727T040837Z_run58 | 11 |
| 20260727T101350Z_run59 | 11 |
| 20260727T151911Z_run60 | 11 |
| 20260727T200002Z_run61 | 11 |
| 20260728T034322Z_run62 | 11 |
| 20260728T091024Z_run63 | 11 |
| 20260728T144449Z_run64 | 11 |
| 20260728T195658Z_run65 | 11 |
| 20260729T005549Z_run66 | 11 |
| 20260729T071735Z_run67 | 11 |
| 20260729T130130Z_run68 | 11 |
| 20260729T184045Z_run69 | 11 |
| 20260730T005240Z_run70 | 11 |
| 20260730T071541Z_run71 | 11 |
| 20260730T125508Z_run72 | 11 |
| 20260730T185142Z_run73 | 11 |
| 20260731T005859Z_run74 | 11 |
| 20260731T072026Z_run75 | 11 |
| 20260731T130035Z_run76 | 11 |
| 20260731T184952Z_run77 | 11 |
| 20260801T005951Z_run78 | 11 |
| 20260801T071136Z_run79 | 11 |
| 20260801T124006Z_run80 | 11 |
| 20260801T184001Z_run81 | 11 |
| 20260802T010032Z_run82 | 11 |
| 20260802T071312Z_run83 | 11 |
| 20260802T124211Z_run84 | 11 |
| 20260802T184025Z_run85 | 11 |
| 20260803T005943Z_run86 | 11 |
| 20260803T073119Z_run87 | 11 |
| 20260803T132256Z_run88 | 11 |
| 20260803T185745Z_run89 | 11 |
| 20260804T005726Z_run90 | 11 |
| 20260804T071554Z_run91 | 11 |
| 20260804T130206Z_run92 | 11 |
| 20260804T185349Z_run93 | 11 |
| 20260805T034031Z_run94 | 11 |
| 20260805T091158Z_run95 | 11 |
| 20260805T143906Z_run96 | 11 |
| 20260805T200004Z_run97 | 11 |
| 20260806T034545Z_run98 | 11 |
| 20260806T091222Z_run99 | 11 |
| 20260806T143946Z_run100 | 11 |
| 20260807T002000Z_run101 | 11 |
| 20260807T074727Z_run102 | 11 |
| 20260807T134347Z_run103 | 11 |
| 20260807T192321Z_run104 | 11 |
| 20260808T022544Z_run105 | 11 |
| 20260808T071931Z_run106 | 11 |
| 20260808T131648Z_run107 | 11 |
| 20260808T190358Z_run108 | 11 |

_Advisory research output. NOT financial advice. No execution, no custody._
