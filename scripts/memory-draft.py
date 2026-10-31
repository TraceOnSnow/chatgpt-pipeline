#!/usr/bin/env python3
"""Run only an explicit finite editorial plan using the existing note workspace."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from chatgpt_pipeline import bulk
from chatgpt_pipeline.notes import Workspace

def main():
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('--workspace',required=True);p.add_argument('--plan',required=True)
 p.add_argument('--report',required=True);p.add_argument('--key-file');p.add_argument('--workers',type=int,default=4)
 p.add_argument('--call',action='store_true');a=p.parse_args()
 if not 1<=a.workers<=4:p.error('workers must be 1..4')
 plan=bulk.read(a.plan)['items'];ids=[x['item_id']for x in plan]
 if not 0<len(ids)<=100 or len(ids)!=len(set(ids)):p.error('explicit distinct plan of 1..100 items required')
 w=Workspace(a.workspace);report=bulk.runtime_path(a.report);records=[];jobs=[]
 for entry in plan:
  try:
   j=w.request(entry['item_id']);jobs.append((entry,j['id']))
  except Exception as e:
   records.append({'group_key':entry.get('group_key'),'item_id':entry['item_id'],'preparation_error':str(e)if isinstance(e,ValueError)else type(e).__name__})
 bulk.save(report,{'called':False,'prepared':len(jobs),'items':records,'usage':w.usage()})
 def run(pair):
  entry,job_id=pair
  return {'group_key':entry.get('group_key'),'item_id':entry['item_id'],**w.run(job_id,a.key_file)}
 if a.call:
  with ThreadPoolExecutor(max_workers=a.workers)as ex:
   for item in ex.map(run,jobs):
    records.append(item);bulk.save(report,{'called':True,'items':records,'usage':w.usage()})
    print(json.dumps({k:item[k]for k in ('group_key','state','error')if k in item},ensure_ascii=False),flush=True)
 else:
  records.extend({'group_key':e.get('group_key'),'item_id':e['item_id'],'job_id':j}for e,j in jobs)
 bulk.save(report,{'called':a.call,'items':records,'usage':w.usage()})
 print(json.dumps({'called':a.call,'planned':len(ids),'prepared':len(jobs),'usage':w.usage()}))
if __name__=='__main__':main()
