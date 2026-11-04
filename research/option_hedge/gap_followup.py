"""Follow-up to gap_ceiling.py (run that first): what a perfectly priced option struck k% away would have paid on the adverse gaps, and the stop-outs that filled past the stop after a session break."""
import os
import pandas as pd
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EVENTS_CSV = '/tmp/option_hedge_gap_events.csv'
ENG={'Selene':(1.0,3.0),'Helios':(20.0,1.6),'Typhon':(500.0,0.8)}
ev=pd.read_csv(EVENTS_CSV,parse_dates=['exit_ts','before','after'])
led=pd.read_csv(f'{REPO}/hestia_backtest/outputs/integrated_trades.csv',parse_dates=['entry_ts','exit_ts'])
led=led[led.engine.isin(ENG)]
led['loss_pct']=-100*led.pts/led.entry
# Tail-only ceiling: what a perfectly priced OTM option struck k% away would pay on the adverse gaps
print('Tail ceiling, Rs per unit over the whole history (x5 for live units), and number of gaps beyond k%:')
rows=[]
for eng,(rpp,stop) in ENG.items():
    e=ev[(ev.engine==eng)&(ev.gap_pct<0)].copy()
    e['rs_per_pct']=e.gap_rs/e.gap_pct
    for k in (0.5,1.0,2.0):
        b=e[e.gap_pct<-k]
        pay=((-b.gap_pct-k)*b.rs_per_pct).sum()
        rows.append((eng,k,len(b),round(pay)))
print(pd.DataFrame(rows,columns=['engine','strike_pct','gaps_beyond','payout_rs_per_unit']).pivot(index='engine',columns='strike_pct',values=['gaps_beyond','payout_rs_per_unit']).to_string())
# stop-outs in the first 30 minutes after a break: compare loss to the stop
print('\nStop-loss exits within 30 min after a session break (loss vs stop):')
for eng,(rpp,stop) in ENG.items():
    t=led[(led.engine==eng)&(led.reason=='stop_loss')]
    e=ev[ev.engine==eng][['trade','after']]
    m=t.merge(e,left_on='trade_id',right_on='trade',how='inner')
    m['min_after']=(m.exit_ts-m.after).dt.total_seconds()/60
    m=m[(m.min_after>=0)&(m.min_after<=30)]
    m['excess']=m.loss_pct-stop
    big=m[m.excess>0.05]
    print(eng,'stop-outs just after a break:',len(m),'| through the stop by >0.05%:',len(big),'| excess Rs/unit sum:',round((big.excess/100*big.entry*rpp).sum()),'| worst excess %:',round(m.excess.max(),2) if len(m) else None)
    if len(big): print(big[['trade_id','exit_ts','min_after','loss_pct','excess']].round(2).to_string(index=False))
# distribution of exit lag after a break for stop-outs (is there a first-minutes exemption?)
print('\nAll stop_loss exits, minutes after the break open (min/median) per engine:')
for eng in ENG:
    t=led[(led.engine==eng)&(led.reason=='stop_loss')]
    m=t.merge(ev[ev.engine==eng][['trade','after']],left_on='trade_id',right_on='trade')
    m['lag']=(m.exit_ts-m.after).dt.total_seconds()/60
    print(eng,len(t),'stop-outs;',len(m),'on a trade that crossed a break; lags of those within 60 min:',sorted(m[(m.lag>=0)&(m.lag<=60)].lag.round().astype(int).tolist())[:20])
