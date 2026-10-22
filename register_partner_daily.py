# -*- coding: utf-8 -*-
"""虚拟钥匙配对健康自愈（每日 cron 执行）：
1. 检查公钥托管可访问性（vtask.top）
2. partner_accounts 重注册（幂等，Tesla 侧记录掉了自动补）
3. 结果落日志 /opt/tesla-fleet-multi/pk-register.log
"""
import json, urllib.request, urllib.parse, ssl, os, time

os.chdir('/opt/tesla-fleet-multi')
ctx = ssl.create_default_context()
LOG = '/opt/tesla-fleet-multi/pk-register.log'
DOMAINS = ['vtask.top', 'pk.vtask.top', 'pk2.vtask.top', 'z13162002608-bit.github.io']
PK_URL = 'https://vtask.top/.well-known/appspecific/com.tesla.3p.public-key'

def log(msg):
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write(time.strftime('%Y-%m-%d %H:%M:%S ') + msg + '\n')

def http(url, data=None, headers=None, timeout=20):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.status, r.read().decode('utf-8', 'replace')

issues = []
# 1) 公钥托管自检
try:
    code, body = http(PK_URL)
    ok = (code == 200 and body.startswith('MF'))
    log(f'公钥自检: {code} {"OK" if ok else "ABNORMAL"}')
    if not ok:
        issues.append(f'公钥托管异常({code})')
except Exception as e:
    log(f'公钥自检 ERR: {e}')
    issues.append(f'公钥托管不可达:{e}')

# 2) partner_accounts 重注册（幂等）
try:
    tok = json.load(open('users/demo01/tokens.json', encoding='utf-8'))
    code, body = http('https://auth.tesla.cn/oauth2/v3/token',
                      urllib.parse.urlencode({
                          'grant_type': 'client_credentials',
                          'client_id': tok.get('client_id'),
                          'client_secret': tok.get('client_secret'),
                          'audience': 'https://fleet-api.prd.cn.vn.cloud.tesla.cn',
                      }).encode(),
                      {'Content-Type': 'application/x-www-form-urlencoded'})
    pt = json.loads(body).get('access_token')
    log(f'伙伴令牌: {"OK" if pt else "FAIL"}')
    for domain in DOMAINS:
        code, body = http(
            'https://fleet-api.prd.cn.vn.cloud.tesla.cn/api/1/partner_accounts',
            json.dumps({'domain': domain}).encode(),
            {'Authorization': 'Bearer ' + (pt or ''), 'Content-Type': 'application/json'})
        log(f'重注册 {domain}: {code}')
        if code != 200:
            issues.append(f'{domain} 重注册 {code}')
except Exception as e:
    log(f'重注册 ERR: {e}')
    issues.append(f'重注册失败:{e}')

log('结论: ' + ('全部正常' if not issues else '异常: ' + '; '.join(issues)))
