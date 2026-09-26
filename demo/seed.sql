-- Demo dataset: support tickets of a fictional Italian SaaS for online shops.
CREATE TABLE IF NOT EXISTS ticket (
  id         bigserial PRIMARY KEY,
  customer   text NOT NULL,
  channel    text NOT NULL,
  body       text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO ticket (customer, channel, body) VALUES
  ('Bottega Verdi', 'email', 'Buongiorno, da stamattina il checkout del nostro shop restituisce errore 500 a ogni tentativo di pagamento. Stiamo perdendo ordini, vi prego di intervenire subito.'),
  ('Sartoria Lume', 'chat', 'Ciao, vorrei cambiare la partita IVA sulle fatture: abbiamo fatto una trasformazione societaria. La nuova è IT09876543210.'),
  ('Cartoleria Nord', 'email', 'Come posso esportare l''elenco clienti in CSV? Non trovo il pulsante nella sezione anagrafiche.'),
  ('Enoteca Rossi', 'telefono', 'La fattura di settembre riporta due volte il canone Pro. Chiedo storno della riga duplicata e nota di credito.'),
  ('Ferramenta Bianchi', 'email', 'Le email di conferma ordine arrivano ai clienti con 3-4 ore di ritardo da ieri. Non è bloccante ma i clienti chiamano preoccupati.'),
  ('Libreria Segni', 'chat', 'Vorrei aggiungere un secondo utente amministratore al nostro account, si può fare dal pannello?'),
  ('Pasticceria Doria', 'email', 'Il plugin per la spedizione con corriere GLS non calcola più le tariffe: mostra sempre 0 euro. Da quando avete aggiornato la piattaforma martedì.'),
  ('Studio Foto Arte', 'telefono', 'Siamo interessati al piano Enterprise, potete mandarci un preventivo per 5 negozi con dominio personalizzato?'),
  ('Officina Meccanica Sud', 'email', 'Non riesco più ad accedere: dice password errata ma sono sicuro sia giusta, e il link di reset non arriva.'),
  ('Vivaio Primavera', 'chat', 'Complimenti per il nuovo editor delle schede prodotto, molto più veloce. Nessun problema, volevo solo dirvelo.'),
  ('Gioielleria Aurea', 'email', 'URGENTE: un cliente ha ricevuto una fattura con i dati di un altro cliente. Problema di privacy, serve una spiegazione entro oggi.'),
  ('Panificio Grano', 'email', 'Vorremmo disdire l''abbonamento a fine mese, chiudiamo l''attività online. Come procediamo?'),
  ('Tessuti Marconi', 'chat', 'Il magazzino mostra 12 pezzi ma ne abbiamo venduti 15 nel weekend: le vendite non hanno scalato le giacenze.'),
  ('Ottica Chiara', 'telefono', 'Ho pagato il rinnovo annuale il 3 del mese ma l''account risulta ancora in prova. Il codice della transazione è TRX-88213.'),
  ('Casa del Caffè', 'email', 'Sarebbe possibile avere le fatture in formato XML per il nostro commercialista invece del solo PDF?'),
  ('Sport Attivo', 'chat', 'La ricerca sul sito non trova i prodotti con accenti nel nome, es. "maglietta térmica". Su desktop e mobile.'),
  ('Erboristeria Salvia', 'email', 'Abbiamo ricevuto un sollecito di pagamento ma la fattura FT-2026-0412 è stata pagata il 12, allego contabile.'),
  ('Bici e Dintorni', 'telefono', 'Vorrei sapere se il piano Base include il modulo per le prenotazioni in negozio o serve il Pro.'),
  ('Arredo Casa Nova', 'email', 'Il sito è irraggiungibile dal nostro dominio arredocasanova.it, ma funziona dal dominio tecnico. Il DNS è configurato come indicato.'),
  ('Farmacia Centrale', 'chat', 'Potete cancellare l''account dell''ex dipendente Marco R.? Ha ancora accesso al pannello ordini.');
