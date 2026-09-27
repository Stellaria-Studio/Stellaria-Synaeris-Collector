import argparse
import json
from synaeris_collector.human_review import review, sample_sheet

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('episode')
    parser.add_argument('--output',required=True)
    parser.add_argument('--samples',type=int,default=0,choices=range(0,25))
    args=parser.parse_args()
    result=review(args.episode,args.output)
    if args.samples:
        sample_sheet(args.episode,args.output,args.samples)
    print(json.dumps({key:value for key,value in result.items() if key!='saved_full_decode_qc'},ensure_ascii=False,indent=2))
