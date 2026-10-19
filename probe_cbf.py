from __future__ import annotations
import json,re
from pathlib import Path
import requests
from bs4 import BeautifulSoup

URLS={
 'detailed':'https://www.cbf.com.br/futebol-brasileiro/tabelas/campeonato-brasileiro/serie-a/2026?documento=Tabela%20Detalhada',
 'table':'https://www.cbf.com.br/futebol-brasileiro/tabelas/campeonato-brasileiro/serie-a/2026',
}
HEADERS={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36','Accept':'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8','Accept-Language':'pt-BR,pt;q=0.9,en-US;q=0.8','Cache-Control':'no-cache'}

def inspect(label,url):
 r=requests.get(url,headers=HEADERS,timeout=45);r.raise_for_status()
 soup=BeautifulSoup(r.text,'html.parser')
 print(f'[{label}] status={r.status_code} bytes={len(r.content)} content_type={r.headers.get("content-type")} title={soup.title.get_text(" ",strip=True) if soup.title else ""}')
 text_all=soup.get_text(' ',strip=True)
 ref_tokens=len(re.findall(r'Ref:\s*',text_all,re.I))
 round_tokens=len(re.findall(r'Rodada',text_all,re.I))
 print(f'[{label}] tables={len(soup.find_all("table"))} trs={len(soup.find_all("tr"))} next_data={soup.find("script",id="__NEXT_DATA__") is not None} ref_tokens={ref_tokens} round_tokens={round_tokens}')
 for i,t in enumerate(soup.find_all('table')):
  rows=[]
  for tr in t.find_all('tr'):
   vals=[c.get_text(' ',strip=True) for c in tr.find_all(['th','td'])]
   if vals:rows.append(vals)
  print(f'[{label}] table#{i} rows={len(rows)} first={rows[:2]} last={rows[-1:] if rows else []}')
  if i>=7:break
 for script in soup.find_all('script'):
  text=script.string or script.get_text() or ''
  if script.get('id')=='__NEXT_DATA__':
   data=json.loads(text);print(f'[{label}] __NEXT_DATA__ top_keys={list(data)[:30]} bytes={len(text)}')
   samples=[]
   def walk(x,path='root'):
    if len(samples)>=25:return
    if isinstance(x,dict):
     for k,v in x.items():walk(v,path+'.'+str(k))
    elif isinstance(x,list):
     if x and isinstance(x[0],dict) and any(('match' in str(k).lower() or 'round' in str(k).lower() or 'fixture' in str(k).lower()) for k in x[0]):samples.append((path,len(x),list(x[0])[:16]))
     for i,v in enumerate(x[:100]):walk(v,path+f'[{i}]')
   walk(data)
   print(f'[{label}] data_list_candidates={samples}')
  elif any(word in text.lower() for word in ['rodada','fixture','partida','classificacao']):
   print(f'[{label}] script id={script.get("id")} type={script.get("type")} bytes={len(text)} sample={text[:250]!r}')
 print(f'[{label}] raw markers: __NEXT_DATA__={r.text.count("__NEXT_DATA__")} application/json={r.text.count("application/json")} fixtureRef={r.text.count("Ref:")}')

for label,url in URLS.items():inspect(label,url)
