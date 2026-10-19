# claude-code-long-sessions

Un piccolo kit per chi lavora con **sessioni lunghe di Claude Code**, o con **più sessioni insieme**.
Fa due cose: tiene bassi i costi in token e impedisce alla sessione di perdere il filo quando la
conversazione viene compattata.

English: [README.md](README.md)

- **`hooks/memory_hook.py`**: un hook che fa tenere a ogni sessione un breve *file di stato*, ricorda al
  modello di aggiornarlo prima della compattazione e lo rimette nel contesto subito dopo.
- **`tools/analyze_usage.py`**: legge i tuoi transcript e mostra dove sono andati i token e quanto
  sarebbero costate altre impostazioni.
- **Modelli e note**: il modello del file di stato, una sezione «Compact instructions» per il tuo
  `CLAUDE.md`, le impostazioni verificate e una procedura per un revisore che guida più sessioni.

Python 3.10 o più recente, solo libreria standard. Provato su Windows; è scritto anche per macOS e Linux,
ma lì non l'abbiamo eseguito.

Progetto indipendente, non affiliato né approvato da Anthropic.

## Il problema

Ogni chiamata al modello **rilegge tutto il contesto** dalla cache: più il contesto cresce, più costa
ogni passo, anche il più piccolo. E quando il contesto viene compattato, il riassunto perde dettagli che
servivano: una decisione, un percorso, un vincolo.

## Che cosa abbiamo misurato

Un caso reale, dal nostro uso: **5 sessioni principali e 34 subagent in 25 ore, 10.530 chiamate all'API,
Claude Code 2.1.302.** I tuoi numeri saranno diversi: `tools/analyze_usage.py` misura i tuoi.

- Il **78%** del costo delle sessioni principali era la rilettura del contesto in cache.
- Che cosa ha fatto partire i turni, pesato sul costo delle sessioni principali: **48%** messaggi da
  altre sessioni, **33%** rilanci del `/goal`, **9%** notifiche, **8%** la persona.
- **1.167** rilanci a vuoto del `/goal` (turni senza nessuno strumento), circa l'**11%** della spesa
  totale. Un revisore in attesa degli altri è stato rilanciato **294 volte in un'ora**, una ogni 12 secondi.
- Compattazione simulata invece che a circa 967k, sul costo delle sessioni principali: **-40%** a 500k,
  **-50%** a 400k, **-56%** a 300k, **-61%** a 200k.
- Subagent: con la cache da 5 minuti il **38%** del loro costo era riscrivere una cache scaduta o
  invalidata.
- Passare le sessioni principali da Opus a Sonnet valeva solo l'**11%** circa: la lettura della cache costa uguale.

## Installazione in 5 minuti

1. **Prendi il kit** (clone o copia della cartella) e annota il percorso assoluto.
2. **Crea la cartella degli stati** e copiaci il modello:
   `mkdir ~/.claude/session-state` e
   `cp templates/STATE_TEMPLATE.md ~/.claude/session-state/_TEMPLATE.md`
   (su Windows `%USERPROFILE%\.claude\session-state`).
3. **Aggiungi hook e impostazioni.** Unisci `hooks/settings.example.json` al tuo `~/.claude/settings.json`,
   sostituendo `/absolute/path/to/claude-code-long-sessions` col percorso vero (su Windows con le barre
   dritte: `C:/Users/me/claude-code-long-sessions`). Nei comandi usa `python`
   (o `python3`, o `py -3` su Windows). Con `/hooks` dentro Claude Code controlla che i quattro hook ci siano.
4. **Incolla le istruzioni di compattazione**: copia `templates/CLAUDE.compact-instructions.md` nel tuo
   `CLAUDE.md`.
5. **Verifica.** `python scripts/integration_check.py` fa una prova completa col modello Haiku (3 chiamate
   brevi): scrive un file di stato, aggiunge da fuori un codice segreto, compatta e chiede il codice alla
   sessione. Con `--dry-run` vedi prima i comandi. I test unitari: `python -m unittest discover -s tests`.
6. **Misura.** `python tools/analyze_usage.py` stampa una tabella per le sessioni in `~/.claude/projects`.

Il blocco `env` dell'esempio imposta `CLAUDE_CODE_AUTO_COMPACT_WINDOW=400000` (compatta prima),
`CLAUDE_CODE_STOP_HOOK_BLOCK_CAP=3` (meno rilanci a vuoto del `/goal` di fila) e
`CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL=1h` (cache più lunga per i subagent).

## Come funziona l'hook

| Comando | Evento | Che cosa fa |
|---|---|---|
| `after-tool` | `PostToolUse` | Se la sessione ha appena scritto un file nella cartella degli stati, lo ricorda come suo file di stato. Quando il contesto si avvicina alla compattazione, ricorda di aggiornarlo (o di crearlo). |
| `pre-compact` | `PreCompact` | Annota la compattazione. Non la blocca mai. |
| `post-summary` | `PostCompact` | Annota se il riassunto cita il file di stato. |
| `after-compact` | `SessionStart` (matcher `compact`) | Rimette il file di stato nel contesto, letto dal disco, con l'istruzione di ripartire dal «Next step». |

Promemoria: il primo a finestra meno 100.000 token, l'urgente a finestra meno 60.000; la compattazione
scatta a circa finestra meno 33.000. Fra un promemoria e l'altro c'è una pausa, e nessun promemoria se il
file è stato aggiornato da poco. L'hook non rompe mai una sessione: a ogni errore non stampa nulla, esce
con 0 e scrive una riga in `errors.log`. I subagent si saltano. Se l'hook non ha visto la scrittura del
file di stato, lo cerca nel transcript fra i file che la sessione ha scritto: un file di stato solo letto,
per esempio quello di un'altra sessione, non viene mai preso.

Variabili dell'hook: `MEMORY_STATE_DIR`, `MEMORY_DATA_DIR`, `MEMORY_FIRST_MARGIN`, `MEMORY_URGENT_MARGIN`,
`MEMORY_THRESHOLD`. Quello che abbiamo verificato sulle variabili di Claude Code è in
[docs/settings.md](docs/settings.md) (in inglese).

## Più sessioni insieme

[docs/multi-session.md](docs/multi-session.md) (in inglese) descrive come far guidare più sessioni
*esecutrici* da una sessione *revisore* senza pagare i risvegli a vuoto: a chi si dà il `/goal`, che cosa
deve contenere un messaggio fra sessioni, come si scrive un incarico a un subagent.

## Analizzare l'uso

```
python tools/analyze_usage.py
python tools/analyze_usage.py --since 2026-01-10 --until 2026-01-12 --out report.json
python tools/analyze_usage.py --session <id sessione> --session <altro id>
```

Deduplica le chiamate (anche fra transcript diversi), separa sessioni principali e subagent, classifica
che cosa ha fatto partire ogni turno pesandolo sul costo, e ripete i tuoi transcript con un'altra soglia di
compattazione, un tetto ai rilanci del `/goal` (un rilancio è a vuoto se nessuna chiamata del suo turno
usa uno strumento) e la cache da 1 ora ai subagent; la tabella degli scenari mostra la differenza sulle
sessioni principali e sul totale. Con `--out report.json` scrive anche chi ha messo i token nuovi nel
contesto. I prezzi sono nella tabella `PRICES`: prezzi di
listino al momento della scrittura, da ricontrollare. Il risultato è un modello del tuo uso, non una fattura.

## Limiti

- Osservato su Claude Code 2.1.302; alcune variabili non sono documentate e possono cambiare.
- Il promemoria arriva solo dopo una chiamata a uno strumento.
- L'hook legge la finestra da `CLAUDE_CODE_AUTO_COMPACT_WINDOW` e, se manca, assume 1.000.000: se le tue
  sessioni compattano prima, imposta la variabile (l'esempio mette 400000).
- L'hook chiede, non obbliga: il file lo scrive il modello e può ignorare il promemoria.
- Se abbassi la finestra sotto il contesto attuale, la compattazione scatta alla chiamata dopo, senza
  tempo per scrivere il file di stato.
- La simulazione è un modello; le nostre percentuali vengono da un solo caso.

## Crediti e licenza

Costruito da AAgency da quello che abbiamo imparato con le sessioni lunghe. Licenza MIT, vedi [LICENSE](LICENSE).
