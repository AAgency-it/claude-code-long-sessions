<p align="center"><img src="docs/img/banner.svg" alt="claude-code-long-sessions: un indicatore del contesto con i promemoria del file di stato a 233k e 273k token, la compattazione intorno a 300k e la ripartenza a circa 145k" width="100%"></p>

# claude-code-long-sessions

Un piccolo kit per chi lavora con **sessioni lunghe di Claude Code**, o con **più sessioni insieme**.
Tiene bassi i costi, tiene una sessione sulla strada giusta quando il suo contesto viene compattato e ti
mostra dove sono andate davvero le ore di un lavoro lungo.

English: [README.md](README.md) · Cosa è cambiato: [CHANGELOG.md](CHANGELOG.md) (in inglese)

| Che cosa | File | Che cosa fa per te |
|---|---|---|
| Hook di memoria | `hooks/memory_hook.py` | Ogni sessione tiene un breve *file di stato*. L'hook ricorda al modello di aggiornarlo prima della compattazione e lo rimette nel contesto subito dopo. |
| Analisi dell'uso | `tools/analyze_usage.py` | Dove sono andati i token, che cosa ha svegliato ogni sessione e quanto sarebbero costate altre impostazioni. |
| Rapporto sul lavoro | `tools/run_report.py` | Dove è andato il *tempo*: ore attive, compattazioni e dimensione della ripartenza, errori degli strumenti per tipo, pause. |
| Divisione dei test | `tools/split_tests.py` | Divide una suite di test in parti di uguale durata, così i processi in parallelo finiscono insieme. |
| Procedure | `docs/` | Un revisore su molti esecutori, lavori pesanti su una sola macchina, le impostazioni che abbiamo verificato, un lavoro di 63 ore misurato da cima a fondo. |
| Modelli | `templates/` | Un file di stato, una sezione «Compact instructions» e cinque regole di lavoro per il tuo `CLAUDE.md`. |

Python 3.10 o più recente, solo libreria standard. Provato su Windows (l'hook capisce anche i percorsi
di Git Bash come `/c/Users/...`); è scritto anche per macOS e Linux, ma lì non l'abbiamo ancora eseguito.
È un progetto indipendente, non affiliato né approvato da Anthropic.

## Il problema

Una sessione lunga costa tanto per un motivo facile da non vedere: **ogni chiamata al modello rilegge
tutto il contesto** dalla cache. Più il contesto è lungo, più costa ogni piccolo passo. Quando il
contesto viene finalmente compattato, il riassunto perde dettagli che servivano, come una decisione, un
percorso o un vincolo, e la sessione spreca i turni successivi per ritrovarli.

Quindi non esiste un'impostazione gratis. Se compatti tardi, paghi la rilettura di un contesto enorme a
ogni chiamata. Se compatti presto, paghi le ripartenze: la cache viene riscritta, il riassunto viene
scritto, i file vengono riletti e alcune modifiche falliscono perché il modello non ha più il file sotto
gli occhi.

## Che cosa abbiamo misurato

Due lavori nostri, letti dai transcript con gli strumenti di questo kit. I tuoi numeri saranno diversi:
gli strumenti misurano i tuoi.

<p align="center"><img src="docs/img/cost-shape.svg" alt="Lavoro 1, compattazione intorno a 967k: la lettura della cache è il 78% del costo. Lavoro 2, compattazione intorno a 188k: scritture della cache 49%, letture della cache 38%, output 13%." width="100%"></p>

**Lavoro 1: 25 ore, 5 sessioni principali, 34 subagent, 10.530 chiamate all'API, Claude Code 2.1.302.**
Compattazione intorno a 967k.

- Il **78%** del costo delle sessioni principali era la rilettura del contesto in cache.
- **1.167** rilanci a vuoto del `/goal`, circa l'**11%** della spesa. Un revisore in attesa degli altri è
  stato rilanciato **294 volte in un'ora**.
- Turni avviati da messaggi di altre sessioni: **48%** del costo delle sessioni principali. Dalla
  persona: **8%**.
- Passare le sessioni principali da Opus a Sonnet valeva solo circa l'**11%**: la lettura della cache costa uguale.

**Lavoro 2: 63 ore, 7 sessioni, 28 subagent, 9.930 turni, Claude Code 2.1.310.** Compattazione intorno a
188k per sei sessioni. Il lavoro ha consegnato 12 rilasci di software, 2 campagne pubblicitarie e 9 clip
video, senza incidenti sui dati.

- **327 compattazioni** hanno richiesto **5,3 ore**, 58 secondi ciascuna. Il riassunto era di circa 52k
  token, ma la prima chiamata dopo portava già circa **145k**: a 188k restavano solo **43k** di spazio
  per lavorare.
- Le **scritture della cache** sono diventate il costo più grande, il **49%**, perché ogni ripartenza
  riscrive la cache.
- Il revisore, che compatta intorno a 367k, è costato il **5% in meno per turno** di una sessione di
  sviluppo che compatta intorno a 188k: meno ripartenze hanno pesato più delle riletture più lunghe.
- **385 errori degli strumenti** in 10.770 chiamate; **189** erano evitabili con una regola già scritta.
- **624 messaggi** fra sessioni, metà da e verso il revisore.
- Nessuna sessione è stata sveglia per **6 ore e 22 minuti** una notte, mentre un rilascio aspettava un via libera.

<p align="center"><img src="docs/img/compaction-threshold.svg" alt="Compattazioni per soglia: 306 a 188k misurate con 43k di spazio, stimate 125 a 250k, 85 a 300k, 59 a 367k." width="100%"></p>

Ora compattiamo intorno a **300k** per le sessioni di lavoro lunghe (finestra 333k) e intorno a 367k per
il revisore (finestra 400k). La storia completa, con ogni leva che abbiamo tirato e quanto ha cambiato:
[docs/run-63h.md](docs/run-63h.md) (in inglese).

<p align="center"><img src="docs/img/tool-errors.svg" alt="385 errori degli strumenti per tipo, 189 evitabili: modifica prima della lettura 115, script lungo in linea bloccato 42, file cambiato dopo la lettura 16, file dei segreti bloccato 16." width="100%"></p>

La lezione dei due lavori: le leve grosse non sono il modello. Sono *quanto diventa lungo il contesto*,
*chi sveglia una sessione*, *quanto spesso scade una cache* e *che cosa fa la macchina mentre le sessioni
aspettano*.

## Installazione in 5 minuti

1. **Prendi il kit** (clone o copia della cartella) e annota il suo percorso assoluto.
2. **Crea la cartella degli stati** e copiaci il modello:

   ```
   mkdir ~/.claude/session-state
   cp templates/STATE_TEMPLATE.md ~/.claude/session-state/_TEMPLATE.md
   ```

   (Su Windows: `%USERPROFILE%\.claude\session-state`.) I file che iniziano con `_` non vengono mai presi per file di stato.
3. **Aggiungi hook e impostazioni.** Unisci `hooks/settings.example.json` al tuo `~/.claude/settings.json`.
   Sostituisci `/absolute/path/to/claude-code-long-sessions` col percorso vero; su Windows scrivilo con le
   barre dritte (`C:/Users/me/claude-code-long-sessions`). Nei comandi usa `python` (o `python3`, o `py -3`
   su Windows), quello che sulla tua macchina avvia Python 3.10 o più recente. Con `/hooks` dentro
   Claude Code controlla che i quattro hook ci siano.
4. **Incolla i modelli nel tuo `CLAUDE.md`**: `templates/CLAUDE.compact-instructions.md` fa iniziare ogni
   riassunto col percorso del file di stato; `templates/CLAUDE.working-rules.md` contiene le cinque
   abitudini che hanno tolto metà dei nostri errori degli strumenti.
5. **Verifica.** `python scripts/integration_check.py` fa una prova completa col modello Haiku (3 chiamate
   brevi): scrive un file di stato, aggiunge da fuori un codice segreto, compatta e chiede il codice alla
   sessione. Con `--dry-run` vedi prima i comandi. I test unitari non usano la rete:
   `python -m unittest discover -s tests`.
6. **Misura.** `python tools/analyze_usage.py` per i token, `python tools/run_report.py` per il tempo.

Il blocco `env` dell'esempio imposta tre cose: `CLAUDE_CODE_AUTO_COMPACT_WINDOW=333000` (compattazione
intorno a 300k), `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP=3` (meno rilanci a vuoto del `/goal` di fila) e
`CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL=1h` (una cache più lunga per i subagent). Cambiale a piacere; vedi
[docs/settings.md](docs/settings.md) (in inglese).

## Come funziona l'hook di memoria

Quattro comandi, uno per ogni evento dell'hook:

| Comando | Evento | Che cosa fa |
|---|---|---|
| `after-tool` | `PostToolUse` | Se la sessione ha appena scritto un file nella cartella degli stati, lo ricorda come file di stato della sessione. Quando il contesto si avvicina alla compattazione, ricorda al modello di aggiornare quel file (o di crearlo). |
| `pre-compact` | `PreCompact` | Annota la compattazione. Non la blocca mai. |
| `post-summary` | `PostCompact` | Annota se il riassunto cita il file di stato. |
| `after-compact` | `SessionStart` (matcher `compact`) | Rimette il file di stato nel contesto, letto dal disco, con l'istruzione di ripartire dal suo «Next step». |

I promemoria sono calcolati sulla finestra (`CLAUDE_CODE_AUTO_COMPACT_WINDOW`, 1.000.000 se non è
impostata): un primo promemoria a finestra meno 100.000 token, uno urgente a finestra meno 60.000, e la
compattazione stessa scatta a circa finestra meno 33.000. Con una finestra da 333k sono 233k, 273k e circa
300k. Fra un promemoria e l'altro c'è una pausa, e nessun promemoria se il file di stato è stato
aggiornato da poco. Le parole sono volutamente ferme («It's a working rule, not a suggestion»).

Il file di stato (`templates/STATE_TEMPLATE.md`) è breve, sotto gli 8.000 caratteri, e si riscrive invece
di allungarlo: obiettivo e fase, fatto, decisioni, in attesa di, prossimo passo, file da rileggere.

Regole di sicurezza dell'hook:

- **Non rompe mai una sessione.** A ogni errore non stampa nulla, esce con 0 e aggiunge una riga a
  `errors.log` nella cartella dei dati.
- **I subagent si saltano.** Non tengono un file di stato.
- Se l'hook non ha visto il momento in cui la sessione ha scritto il suo file di stato, lo cerca nel
  transcript: l'ultimo file di stato che la sessione ha scritto (Write o Edit). Un file di stato solo
  letto dalla sessione, per esempio quello di un'altra sessione, non viene mai preso.

## Gli strumenti

### Dove sono andati i token: `tools/analyze_usage.py`

```
python tools/analyze_usage.py                              # tutto ciò che sta in ~/.claude/projects
python tools/analyze_usage.py --since 2026-01-10 --until 2026-01-12 --out report.json
python tools/analyze_usage.py --session <session id> --session <another id>
```

Deduplica le chiamate per id del messaggio, separa le sessioni principali dai subagent e classifica che
cosa ha fatto partire ogni turno, pesato sul costo. Ripete i tuoi transcript con un'altra soglia di
compattazione (`--thresholds`), un tetto ai rilanci a vuoto del `/goal` (`--rerun-cap`, predefinito 3) e
una cache da 1 ora per i subagent; la tabella degli scenari mostra la differenza sulle sessioni
principali e sul totale. I costi usano la tabella `PRICES` in cima al file: prezzi di listino al momento
della scrittura, da ricontrollare. Il risultato è un modello del tuo uso, non una fattura.

### Dove è andato il tempo: `tools/run_report.py`

```
python tools/run_report.py --since 2026-01-10T08:00 --until 2026-01-12T20:00
python tools/run_report.py --session <session id> --session <another id> --out run.json
```

Per ogni sessione: ore di calendario e ore attive (le pause sotto i 5 minuti contano come lavoro), turni,
secondi per turno, compattazioni (il contesto a cui sono scattate, la dimensione del riassunto, il
contesto della prima risposta dopo di esso e lo spazio rimasto fra i due), minuti passati a compattare,
errori degli strumenti raggruppati per tipo con quelli evitabili contati a parte, messaggi ad altre
sessioni e ogni pausa più lunga di un'ora. I subagent sono riassunti a parte. Gli orari sono in UTC.
Non viene stampato nessun testo delle conversazioni: solo conteggi e la prima riga di ogni messaggio di
errore, con percorsi e numeri oscurati.

```
session   hours active h turns s/turn compactions compact at summary restart at room compaction min
dev-1      50.9     17.3  2956     21         123       188k     52k       145k  43k          124.0
```

### Parti di uguale durata: `tools/split_tests.py`

```
pytest --junitxml=junit.xml                                # una volta, per misurare
python tools/split_tests.py timings junit.xml --out timings.json
python tools/split_tests.py plan --glob 'tests/test_*.py' --timings timings.json --parts 6
pytest $(python tools/split_tests.py plan --glob 'tests/test_*.py' --timings timings.json --parts 6 --part 3)
```

Una suite lanciata in parallelo dura quanto la sua parte più lunga. Dividendo per nome di file avevamo
parti da 523 a 1.045 secondi; dividendo per tempo misurato l'intera suite è passata da 17 min 37 s a
**13 min 52 s (-21%)** sulla stessa macchina. Il pianificatore mette il file più lento per primo nella
parte più vuota; un file senza tempo pesa quanto la mediana, e ti avvisa quando troppi file non ce l'hanno.

## Più di una sessione, e lavori pesanti

- [docs/multi-session.md](docs/multi-session.md) (in inglese): un *revisore* su più *esecutori* senza
  pagare perché si sveglino a vicenda. Chi tiene il `/goal`, che cosa deve contenere un messaggio, via
  libera a lotti, come scrivere un incarico a un subagent.
- [docs/heavy-jobs.md](docs/heavy-jobs.md) (in inglese): suite di test, render e build lanciati da più
  sessioni su una sola macchina. Una coda sola, un tetto di memoria per ogni lavoro, più processi invece
  di più thread, il lavoro della notte in un solo script, una revisione indipendente accanto ai test.
- [docs/run-63h.md](docs/run-63h.md) (in inglese): il lavoro di 63 ore, le leve, che cosa abbiamo cambiato e che cosa ha dato.

<p align="center"><img src="docs/img/levers.svg" alt="Ore risparmiate stimate per leva: lavoro della notte in un solo script 5 ore di calendario, compattazione intorno a 300k 3,6, una coda sola per i lavori pesanti 2,5, via libera a lotti 1,8, regole contro gli errori evitabili 1,1, nessun test noto che fallisce 0,3." width="100%"></p>

## Limiti

- **Osservato su Claude Code 2.1.302 (lavoro 1) e 2.1.310 (lavoro 2).** Alcune variabili non sono
  documentate e possono cambiare in altre versioni.
- **Il promemoria dipende dalle chiamate agli strumenti.** L'hook gira dopo ogni chiamata a uno strumento;
  una sessione che risponde a lungo senza usare uno strumento non riceve nessun promemoria fino alla
  chiamata successiva.
- **L'hook deve conoscere la tua finestra.** Legge `CLAUDE_CODE_AUTO_COMPACT_WINDOW` e assume 1.000.000
  quando non è impostata. Se le tue sessioni compattano prima, imposta la variabile, altrimenti i
  promemoria arrivano troppo tardi o mai.
- **Chiede, non obbliga.** Il file di stato lo scrive il modello; l'hook può solo ricordarglielo e
  rimettere il file nel contesto. Un modello può comunque ignorare il promemoria.
- **Abbassare la finestra sotto il contesto attuale** fa scattare la compattazione alla chiamata
  successiva, senza tempo per scrivere il file di stato.
- **Tetto del contesto.** Claude Code taglia il contesto iniettato a 10.000 caratteri; l'hook resta sotto
  i 9.500 e taglia un file di stato più lungo lasciando l'indicazione di aprirlo.
- **Le stime sono modelli.** La simulazione della compattazione assume che il contesto cresca della stessa
  quantità a ogni chiamata; i risparmi per leva nel rapporto sul lavoro sono stime e si sovrappongono.
  Usali per confrontare le impostazioni, non per prevedere una fattura.
- **Le nostre percentuali vengono da due lavori.** Un carico di lavoro diverso dà quote diverse.

## Test

```
python -m unittest discover -s tests
```

I test lanciano gli script veri come li lanciano Claude Code e una shell (JSON su stdin, file su disco) e
leggono transcript e rapporti di test sintetici in `tests/fixtures`.

## Crediti e licenza

Costruito da AAgency da quello che abbiamo imparato con le sessioni lunghe. Licenza MIT, vedi [LICENSE](LICENSE).
