"""컨테이너 안: 시트 목록을 운영 paper 측정 시뮬레이터(분봉 혼합)로 잰다. DB 쓰기 없음."""

import asyncio
import json
import sys
from datetime import date

import asyncpg

from prime_jennie_runtime.infra.config import PostgresConfig
from prime_jennie_runtime.jobs import paper_outcomes as po
from prime_jennie_runtime.position_sheet.schema import PositionSheet


async def main():
    pg = PostgresConfig()
    pool = await asyncpg.create_pool(
        host=pg.host,
        port=pg.port,
        user=pg.user,
        password=pg.password,
        database=pg.db,
        min_size=1,
        max_size=4,
    )
    out = open(sys.argv[2], "w")
    n = 0
    for line in open(sys.argv[1]):
        j = json.loads(line)
        s = PositionSheet.model_validate(j["sheet"])
        o = await po._simulate_sheet(pool, s, today=date(2026, 10, 9))
        if o is None:
            continue
        out.write(
            json.dumps(
                {
                    "grp": j["grp"],
                    "tag": j["tag"],
                    **{
                        k: (
                            str(v)
                            if not isinstance(v, (int, float, str, type(None), list, dict))
                            else v
                        )
                        for k, v in o.items()
                    },
                },
                default=str,
            )
            + "\n"
        )
        n += 1
    print("done", n)


asyncio.run(main())
