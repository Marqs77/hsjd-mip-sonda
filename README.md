# Sonda de disponibilitat · SJD-MIP (HSJD)

Comprova des de fora, cada minut, que la plataforma de prova de l'Hospital Sant Joan de Déu respon
(web, API + base de dades, ONLYOFFICE i certificat) i envia el resultat al tauler
**Qualitat del servei** (`/monitor/#servei`), que calcula l'SLA mensual (objectiu 99,5 %, 24x7).

- No fa servir secrets: s'identifica amb el token OIDC de GitHub Actions d'aquest repositori.
- No desa dades de l'aplicació: només hores, temps de resposta i si ha anat bé.
- Si la plataforma no respon, els resultats es guarden a `estat/` i s'envien quan torna.
- Si la plataforma cau 2 minuts seguits o es recupera, avisa per correu i SMS a través d'Azure (credencial federada, sense secrets).
