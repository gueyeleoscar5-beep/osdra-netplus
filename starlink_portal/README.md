# osdra_NetPlus

Application web Flask pour gérer les kits Starlink de tes clients :
- suivi des clients et des kits
- affichage explicite du nom du client dans le tableau de bord et l’espace public
- dates d’expiration
- enregistrement des paiements mensuels
- lien public par client pour consulter son abonnement
- rappels WhatsApp automatiques aux jours J+10, J+20, J+24, J+25, J+26, J+27, J+28, J+29 et J+30 après expiration
- message WhatsApp immédiat quand un paiement est enregistré
- logo intégré pour la marque osdra_NetPlus

## Messages WhatsApp configurés

### PAYÉ
`Cher client {nom_du_client}, votre abonnement est actif jusqu'au {date_du_30e_jour}.`

### NON PAYÉ
- J+10, J+20, J+24 : rappel simple d’impayé
- J+25 à J+30 :
`Cher client {nom_du_client}, nous vous signalons le non paiement de votre abonnement. Votre kit sera bloqué et nous nous désengageons de toutes les conséquences.`

Le lien public de suivi du client est aussi ajouté dans le message WhatsApp.

## Installation locale

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scriptsctivate
pip install -r requirements.txt
cp .env.example .env
python app.py
```

Ouvre ensuite : `http://127.0.0.1:5000`

## Variables importantes

Ajoute ces variables dans `.env` :

```env
APP_NAME=osdra_NetPlus
APP_TAGLINE=Suivi des kits Starlink, paiements, expirations et alertes WhatsApp
BASE_URL=https://osdra-netplus.com
APP_TIMEZONE=Africa/Bangui
PAYMENT_VALIDITY_DAYS=30
REMINDER_DAYS=10,20,24,25,26,27,28,29,30
REMINDER_HOUR=8
REMINDER_MINUTE=0
TWILIO_ENABLED=false
ADMIN_USERNAME=osdraadmin
ADMIN_PASSWORD=CHANGE_ME_STRONG_PASSWORD
```

Un modèle déjà prérempli est fourni ici : `.env.osdra-netplus.example`

