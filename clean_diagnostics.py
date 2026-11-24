"""Durable per-bubble CLEAN evidence. Contains local paths, never cloud credentials."""
import json
import os
import uuid
from datetime import datetime
from pathlib import Path

COMPONENT_VERSION='4.0'


def counts(records,total):
    result={'total':total,'attempted':len(records),'success':0,'review':0,'failed':0,'cancelled':0}
    for record in records:result[record['status']]+=1
    result['pending']=total-len(records)
    result['outputs']=result['success']+result['review']
    return result


def summary_text(summary):
    return (f"ประมวลผล {summary['attempted']}/{summary['total']} กรอบ • "
            f"สำเร็จตามการตรวจ {summary['success']} • ต้องตรวจเพิ่ม {summary['review']} • "
            f"ล้มเหลว {summary['failed']} • ยกเลิกระหว่างทำ {summary['cancelled']} • ยังไม่ทำ {summary['pending']} "
            f"(ได้ภาพ {summary['outputs']} = ผ่านการตรวจ + ต้องตรวจเพิ่ม)")


class CleanRun:
    def __init__(self,folder,total,readiness):
        self.folder=Path(folder)/(datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8])
        self.folder.mkdir(parents=True,exist_ok=False)
        self.total=total;self.records=[]
        self.write_json('run.json',{'component_version':COMPONENT_VERSION,'total':total,'lama':readiness})

    def write_json(self,name,data,compact=False):
        destination=self.folder/name;temporary=destination.with_suffix('.tmp')
        try:
            payload=(json.dumps(data,ensure_ascii=False,separators=(',',':'),allow_nan=False)
                     if compact else json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False))
            temporary.write_text(payload,encoding='utf-8')
            os.replace(temporary,destination)
        finally:temporary.unlink(missing_ok=True)

    def append(self,record,region=None):
        if region is not None:
            # Failure/review evidence remains editable via the saved project too.
            from bubble_cleaner import encode_region
            name=f"region-{record['index']:04d}.json"
            self.write_json(name,encode_region(region),compact=True);record['draft_file']=name
        with (self.folder/'regions.jsonl').open('a',encoding='utf-8') as stream:
            stream.write(json.dumps(record,ensure_ascii=False,allow_nan=False)+'\n')
            stream.flush()
        self.records.append(record)

    def finish(self):
        result=counts(self.records,self.total)
        self.write_json('summary.json',result)
        return summary_text(result)
