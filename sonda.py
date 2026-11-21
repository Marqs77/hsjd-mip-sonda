#!/usr/bin/env python3
"""Sonda externa de disponibilitat de l'SJD-MIP (Hospital Sant Joan de Déu · prova d'usabilitat).

Cada minut, durant ~9 minuts, comprova des de fora (GitHub Actions) cada entorn:
  web         → la pàgina principal respon 200 (nginx + certificat)
  api         → /api/sla/ping respon 200 (API + base de dades)
  onlyoffice  → /onlyoffice/healthcheck == true (només degrada el servei)
  cert_days   → dies que falten perquè caduqui el certificat HTTPS
Cada comprovació es reintenta fins a 3 vegades abans de donar-la per fallida.

Els resultats s'envien a l'API de cada entorn amb un token OIDC de GitHub (sense secrets).
Si l'entorn no respon, es guarden a estat/pendents-<entorn>.jsonl i s'envien quan torna.
Si l'entorn principal cau (2 minuts seguits) o es recupera, es pot avisar per Azure (correu + SMS)
escrivint una etiqueta en un grup de recursos vigilat per una alerta del registre d'activitat.
No es desa cap dada de l'aplicació: només hores, temps de resposta i si ha anat bé.
"""
import json, os, socket, ssl, sys, time, urllib.parse, urllib.request
from datetime import datetime, timezone

TARGETS = json.loads(os.environ.get('TARGETS', '[]'))
DUR, EVERY, AUD = int(os.environ.get('DURATION', '540')), 60, os.environ.get('AUDIENCE', 'hsjd-mip-sla')
MAIN = os.environ.get('MAIN_ENV', 'prova')
ST = 'estat'


def utc():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def http(url, timeout=10, data=None, headers=None, method=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    t = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read(200000), round((time.time() - t) * 1000)


def check(name, fn):
    err = None
    for i in range(3):
        try:
            ok, ms, st = fn()
            if ok:
                return {'name': name, 'ok': True, 'ms': ms, 'status': st}
            err = f'HTTP {st}'
        except Exception as e:  # noqa: BLE001
            err = type(e).__name__ + ': ' + str(e)[:150]
        if i < 2:
            time.sleep(5)
    return {'name': name, 'ok': False, 'error': err}


def cert_days(base):
    u = urllib.parse.urlparse(base)
    try:
        with socket.create_connection((u.hostname, u.port or 443), timeout=10) as s:
            with ssl.create_default_context().wrap_socket(s, server_hostname=u.hostname) as c:
                exp = ssl.cert_time_to_seconds(c.getpeercert()['notAfter'])
        return int((exp - time.time()) // 86400)
    except Exception:  # noqa: BLE001
        return None


def probe(base):
    def web():
        st, body, ms = http(base + '/')
        return st == 200 and b'<html' in body.lower()[:5000], ms, st

    def api():
        st, body, ms = http(base + '/api/sla/ping')
        return st == 200 and json.loads(body).get('ok') is True, ms, st

    def oo():
        st, body, ms = http(base + '/onlyoffice/healthcheck')
        return st == 200 and body.strip() == b'true', ms, st

    return {'ts': utc(), 'source': 'github', 'run': os.environ.get('GITHUB_RUN_ID', '')[:40],
            'checks': [check('web', web), check('api', api), check('onlyoffice', oo)], 'cert_days': cert_days(base)}


def oidc(aud):
    u, t = os.environ.get('ACTIONS_ID_TOKEN_REQUEST_URL'), os.environ.get('ACTIONS_ID_TOKEN_REQUEST_TOKEN')
    if not u:
        return None
    _, body, _ = http(u + '&audience=' + urllib.parse.quote(aud), headers={'Authorization': 'bearer ' + t})
    return json.loads(body)['value']


def send(base, items):
    tok = oidc(AUD)
    st, body, _ = http(base + '/api/sla/probe', timeout=30, data=json.dumps(items).encode(), method='POST',
                       headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + (tok or '')})
    return st == 200


# ---------------------------------------------------------------- avís per Azure (opcional)
def azure_signal(kind, detail):
    """Escriu una etiqueta al grup de recursos AZ_RG_<kind>; una alerta del registre d'activitat envia correu + SMS."""
    ten, cli, sub, rg = (os.environ.get(k, '') for k in ('AZ_TENANT', 'AZ_CLIENT', 'AZ_SUB', 'AZ_RG_' + kind.upper()))
    if not (ten and cli and sub and rg):
        return
    try:
        ass = oidc('api://AzureADTokenExchange')
        data = urllib.parse.urlencode({'client_id': cli, 'grant_type': 'client_credentials', 'scope': 'https://management.azure.com/.default',
                                       'client_assertion_type': 'urn:ietf:params:oauth:client-assertion-type:jwt-bearer', 'client_assertion': ass}).encode()
        _, body, _ = http(f'https://login.microsoftonline.com/{ten}/oauth2/v2.0/token', data=data)
        at = json.loads(body)['access_token']
        url = f'https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Resources/tags/default?api-version=2021-04-01'
        http(url, data=json.dumps({'properties': {'tags': {'estat': kind, 'quan': utc(), 'detall': detail[:250]}}}).encode(), method='PUT',
             headers={'Authorization': 'Bearer ' + at, 'Content-Type': 'application/json'})
        print(f'::notice::Avís Azure enviat: {kind}')
    except Exception as e:  # noqa: BLE001
        print(f'::warning::No s\'ha pogut avisar per Azure: {e}')


def load(p, default):
    try:
        with open(p) as f:
            return json.load(f) if p.endswith('.json') else [json.loads(l) for l in f if l.strip()]
    except (OSError, ValueError):
        return default


def main():
    os.makedirs(ST, exist_ok=True)
    pend = {t['env']: load(f'{ST}/pendents-{t["env"]}.jsonl', []) for t in TARGETS}
    state = load(f'{ST}/estat.json', {})
    fails = state.get('fails', 0)
    t_end = time.time() + DUR
    last_main_ok = True
    while True:
        t0 = time.time()
        for t in TARGETS:
            r = probe(t['base'])
            core = all(c['ok'] for c in r['checks'] if c['name'] in ('web', 'api'))
            print(f"{r['ts']} {t['env']:9} {'OK ' if core else 'KO '} " + ' '.join(f"{c['name']}={'ok' if c['ok'] else 'KO'}" + (f"({c['ms']}ms)" if c.get('ms') else '') for c in r['checks']) + f" cert={r['cert_days']}d")
            pend[t['env']].append(r)
            try:
                if send(t['base'], pend[t['env']][-2000:]):
                    pend[t['env']] = []
            except Exception as e:  # noqa: BLE001
                print(f'::warning::{t["env"]}: no s\'han pogut enviar {len(pend[t["env"]])} resultats ({e})')
            if t['env'] == MAIN:
                last_main_ok = core
                fails = 0 if core else fails + 1
                if fails == 2 and not state.get('down'):
                    state['down'] = r['ts']
                    bad = '; '.join(f"{c['name']}: {c.get('error')}" for c in r['checks'] if not c['ok'])
                    azure_signal('caiguda', f'{t["base"]} no respon des de {r["ts"]}: {bad}')
                if core and state.get('down'):
                    azure_signal('recuperat', f'{t["base"]} torna a respondre ({r["ts"]}); caigut des de {state["down"]}')
                    state['down'] = None
        if time.time() + EVERY > t_end:
            break
        time.sleep(max(0, EVERY - (time.time() - t0)))
    state.update(fails=fails, dia=utc()[:10])
    for env, items in pend.items():
        p = f'{ST}/pendents-{env}.jsonl'
        if items:
            with open(p, 'w') as f:
                f.write('\n'.join(json.dumps(x) for x in items[-5000:]) + '\n')
        elif os.path.exists(p):
            os.remove(p)
    with open(f'{ST}/estat.json', 'w') as f:
        json.dump(state, f, indent=1)
    if not last_main_ok:
        print(f'::error::{MAIN} no és disponible')
        sys.exit(1)


if __name__ == '__main__':
    main()
