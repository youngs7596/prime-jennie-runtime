\pset footer off
create temp table cal as select price_date d, row_number() over (order by price_date) i from daily_prices where stock_code='069500' and price_date>='2026-08-01';
create temp table mr as select distinct on (score_date) score_date, run_id from daily_quant_scores where score_date >= '2026-09-01' and run_id like 'sr_%_0830_%' order by score_date, run_id;
create temp table g as select (generated_at at time zone 'Asia/Seoul')::date gd, gate from macro_runs where trigger_reason like 'scheduled%' and to_char(generated_at at time zone 'Asia/Seoul','HH24MI') between '0825' and '0845';
create temp table x as
select q.score_date e0, q.stock_code, q.is_final_selected sel, q.total_quant_score sc, case when q.score_date>='2026-10-12' then '@3' else '@2' end ver,
  coalesce((select gate from g where gd=q.score_date limit 1),'?') gate,
  (u5.close_price::float-u0.open_price)/u0.open_price*100 ret
from daily_quant_scores q join mr on mr.run_id=q.run_id
join cal c0 on c0.d=q.score_date join cal c5 on c5.i=c0.i+5
join daily_prices u0 on u0.stock_code=q.stock_code and u0.price_date=q.score_date and u0.open_price>0
join daily_prices u5 on u5.stock_code=q.stock_code and u5.price_date=c5.d;
create temp table dd as
select ver, e0, gate, avg(ret) filter (where sel) s, avg(ret) u, count(*) filter (where sel) n from x group by 1,2,3;
select ver, gate, count(*) days, sum(n) n, round(avg(s)::numeric,2) sel_t5, round(avg(u)::numeric,2) univ_t5, round(avg(s-u)::numeric,2) alpha_day, round((avg(s-u)/nullif(stddev(s-u)/sqrt(count(*)),0))::numeric,2) t_day, sum((s>u)::int) pos
from dd group by rollup(1,2) order by 1,2;
select e0, gate, n, round(s::numeric,2) sel, round(u::numeric,2) univ, round((s-u)::numeric,2) a from dd where ver='@3' order by 1;
-- 점수 단조성(@3, 유니버스 전체, 날짜 내 순위 5분위)
select ver, qn, count(*), round(avg(ret - dm)::numeric,2) excess from (select x.*, ntile(5) over (partition by e0 order by sc) qn, avg(ret) over (partition by e0) dm from x) z group by 1,2 order by 1,2;
