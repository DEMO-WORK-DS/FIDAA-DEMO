# Willkommen bei FIDAA!

Ich bin **FIDAA** (*Fachinformation Digitale Aufsuchende Arbeit*) – der Kompetenzbot des
Forschungs- und Transferprojekts [DEMO-WORK](https://demo-work.h2.de). Ich beantworte Fragen zu
**Digital Streetwork** – aufsuchende Arbeit in digitalen Räumen – unter anderem zu
Radikalisierungsprävention, Empowerment und Netzwerkarbeit.

Ich informiere auf Basis einer kuratierten, fachlich geprüften Wissensdatenbank: Jede Antwort
entsteht aus dem von mir recherchierten Kontext (sichtbar in den Suchschritten) mit Quellenangaben
aus der Fachliteratur. Ich informiere und verweise auf Anlaufstellen, erbringe aber **keine Beratung**
(keine Seelsorge, Rechts- oder psychologische Unterstützung).

Stellen Sie mir einfach eine Frage – oder wählen Sie unten einen Vorschlag aus.

## Projekt & Kontakt

FIDAA ist Teil von **DEMO-WORK** – einem von der **VolkswagenStiftung** geförderten Forschungs- und
Transferprojekt in Kooperation zwischen der Hochschule Magdeburg-Stendal, der Amadeu Antonio
Stiftung und der Katholischen Hochschule Nordrhein-Westfalen (katho).

- **Web-App (Sie sind hier):** [fidaa.h2.de](https://fidaa.h2.de)
- **Projektseite** (Projektinformationen, Team, Kontakt/Impressum): [demo-work.h2.de](https://demo-work.h2.de)
  (Englisch: [demo-work.h2.de/en/](https://demo-work.h2.de/en/))

## Open Source & Wiederverwendung

Die Software ist Open Source (Code: EUPL 1.2, Wissensinhalte: CC BY-SA 4.0) und auf GitHub in zwei
Repositories geteilt:

- **Wissenspaket + MCP-Server:** [github.com/DEMO-WORK-DS/FIDAA](https://github.com/DEMO-WORK-DS/FIDAA) –
  das Fachwissen plus ein MCP-Server, der es als Standard-Tools (Suche, Inhaltsverzeichnis,
  Kapiteltexte) an KI-Agenten weitergibt.
- **Demo-App (Chainlit):** [github.com/DEMO-WORK-DS/FIDAA-DEMO](https://github.com/DEMO-WORK-DS/FIDAA-DEMO) –
  diese Chat-Oberfläche, die auf dem Wissenspaket aufsetzt.

**FIDAA in eigene Chatbots einbinden:** Das Fachwissen ist als eigenständiges Wissenspaket gebaut –
andere Chatbots und KI-Agenten können dasselbe Wissen nutzen, indem sie einfach auf das
[GitHub-Repository](https://github.com/DEMO-WORK-DS/FIDAA) zeigen. `AGENTS.md` und `SKILL.md`
beschreiben dort die Anbindung (MCP-Server oder direkte Nutzung der `knowledge/`-Dateien); das
Wissen wird damit nicht dupliziert, sondern einmal gepflegt und überall nutzbar.
