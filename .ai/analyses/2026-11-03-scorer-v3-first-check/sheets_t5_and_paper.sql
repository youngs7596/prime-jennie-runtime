\pset footer off
create temp table cal as select price_date d, row_number() over (order by price_date) i from daily_prices where stock_code='069500' and price_date>='2026-08-01';
create temp table s as
select ps.sheet_id, ps.ticker, sr.prompt_version ver, (ps.generated_at at time zone 'Asia/Seoul') pubts,
  (select min(d) from cal where d >= (ps.generated_at at time zone 'Asia/Seoul')::date) e0, po.pnl_pct::float pnl, po.net_pnl_pct::float net, po.exit_reason
from position_sheets ps join scout_runs sr on sr.scout_run_id = ps.provenance_json->>'scout_run_id'
left join paper_outcomes po on po.sheet_id=ps.sheet_id
where ps.generated_at >= '2026-09-01';
-- 발행이 장 마감 뒤면 다음 거래일 진입
update s set e0 = (select min(d) from cal where d > s.e0) where pubts::time >= '15:20' and pubts::date = e0;
create temp table h as
select s.*, (select d from cal c2 where c2.i = c1.i+5) e5 from s join cal c1 on c1.d=s.e0;
create temp table r as
select h.*, 
 (p5.close_price::float - p0.open_price)/p0.open_price*100 ret,
 (b5.close_price::float - b0.open_price)/b0.open_price*100 bench,
 (select avg((u5.close_price::float-u0.open_price)/u0.open_price*100) from (select distinct stock_code from daily_quant_scores q where q.score_date=h.e0) uq
    join daily_prices u0 on u0.stock_code=uq.stock_code and u0.price_date=h.e0 and u0.open_price>0
    join daily_prices u5 on u5.stock_code=uq.stock_code and u5.price_date=h.e5) univ
from h
join daily_prices p0 on p0.stock_code=h.ticker and p0.price_date=h.e0 and p0.open_price>0
join daily_prices p5 on p5.stock_code=h.ticker and p5.price_date=h.e5
join daily_prices b0 on b0.stock_code='069500' and b0.price_date=h.e0
join daily_prices b5 on b5.stock_code='069500' and b5.price_date=h.e5
where h.e5 is not null;
select ver, count(*) n, count(distinct ticker) tickers, min(e0), max(e0),
 round(avg(ret)::numeric,2) t5, round(avg(bench)::numeric,2) kodex, round(avg(univ)::numeric,2) univ,
 round(avg(ret-bench)::numeric,2) a_kodex, round(avg(ret-univ)::numeric,2) a_univ,
 round((avg(ret-univ)/(stddev(ret-univ)/sqrt(count(*))))::numeric,2) t_naive,
 round(100.0*avg((ret>univ)::int),0) beat_univ
from r group by 1 order by 1;
-- 날짜별 (같은 날 시트 평균) — 날짜 군집 고려
select ver, count(*) days, round(avg(a)::numeric,2) a_univ_daymean, round((avg(a)/(stddev(a)/sqrt(count(*))))::numeric,2) t_day, round(100.0*avg((a>0)::int)) pos_days
from (select ver, e0, avg(ret-univ) a from r group by 1,2) x group by 1 order by 1;
select e0, count(*) n, round(avg(ret)::numeric,2) t5, round(avg(univ)::numeric,2) univ, round(avg(bench)::numeric,2) kodex, round(avg(ret-univ)::numeric,2) a_univ from r where ver like '%@3' group by 1 order by 1;
-- 측정(청산) 기준
select ver, count(pnl) n, round(avg(pnl)::numeric,2) pnl, round(avg(net)::numeric,2) net, round(100.0*avg((pnl>0)::int)) win, round(100.0*avg((exit_reason='fixed_sl')::int)) sl_pct from s where pnl is not null and e0 <= '2026-10-27' group by 1 order by 1;
