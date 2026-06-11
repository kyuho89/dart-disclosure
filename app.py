# -*- coding: utf-8 -*-
"""DART 공시 조회 Streamlit 앱 (OpenDART API 사용)"""

import os
import re
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

CORP_SEARCH_URL = "https://dart.fss.or.kr/dsae001/search.ax"
LIST_URL = "https://opendart.fss.or.kr/api/list.json"
VIEWER_URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo="

DEFAULT_COMPANY = "브이엠"
WATCHLIST_FILE = Path(__file__).parent / "watchlist.txt"
DEFAULT_WATCHLIST = "089970"
API_KEY_FILE = Path(__file__).parent / ".dart_api_key"

# OpenDART 공시유형 코드
PBLNTF_TY = {
    "전체": None,
    "정기공시": "A",
    "주요사항보고": "B",
    "발행공시": "C",
    "지분공시": "D",
    "기타공시": "E",
    "외부감사관련": "F",
    "펀드공시": "G",
    "자산유동화": "H",
    "거래소공시": "I",
    "공정위공시": "J",
}


# ──────────────────────────────────────────────────────────
# 데이터 조회
# ──────────────────────────────────────────────────────────
@st.cache_data(ttl=3600, show_spinner="회사 검색 중...")
def search_companies(keyword: str) -> pd.DataFrame:
    """DART 사이트 회사검색. 회사명/종목코드 모두 검색 가능 (API 키 불필요)."""
    resp = requests.post(
        CORP_SEARCH_URL,
        data={"currentPage": 1, "maxResults": 100, "textCrpNm": keyword.strip()},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=30,
    )
    resp.raise_for_status()

    rows = []
    for tr in resp.text.split("<tr>")[1:]:
        code_m = re.search(r"select\('(\d{8})'\)", tr)
        name_m = re.search(r'title="(.+?) 기업개황', tr)
        if not (code_m and name_m):
            continue
        stock_m = re.search(r"<td>\s*(\d{6})\s*</td>", tr)
        market_m = re.search(r'tagCom_\w+"?\s+title="([^"]+)"', tr)
        rows.append(
            {
                "corp_code": code_m.group(1),
                "corp_name": name_m.group(1).strip(),
                "stock_code": stock_m.group(1) if stock_m else "",
                "market": market_m.group(1) if market_m else "",
            }
        )
    return pd.DataFrame(rows, columns=["corp_code", "corp_name", "stock_code", "market"])


def find_corp_by_name(name: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """입력한 이름과 '정확히 일치'하는 회사만 반환. (일치 목록, 유사 목록)"""
    name = name.strip()
    all_matches = search_companies(name)
    if all_matches.empty:
        return all_matches, all_matches
    exact = all_matches[all_matches["corp_name"] == name]
    similar = all_matches[all_matches["corp_name"] != name]
    # 동명 법인이 여러 개면 상장사 우선
    return exact.sort_values("stock_code", ascending=False), similar


def find_corp_by_stock_code(stock_code: str) -> pd.Series | None:
    """종목코드(6자리)와 정확히 일치하는 상장사 반환. 없으면 None."""
    matches = search_companies(stock_code)
    exact = matches[matches["stock_code"] == stock_code]
    return exact.iloc[0] if not exact.empty else None


@st.cache_data(ttl=600, show_spinner="공시 목록 조회 중...")
def fetch_disclosures(
    api_key: str,
    corp_code: str,
    bgn_de: str,
    end_de: str,
    pblntf_ty: str | None,
) -> pd.DataFrame:
    """공시검색 API(list.json)를 페이지 끝까지 조회해 DataFrame으로 반환."""
    all_rows = []
    page_no = 1
    while True:
        params = {
            "crtfc_key": api_key,
            "corp_code": corp_code,
            "bgn_de": bgn_de,
            "end_de": end_de,
            "page_no": page_no,
            "page_count": 100,
        }
        if pblntf_ty:
            params["pblntf_ty"] = pblntf_ty

        resp = requests.get(LIST_URL, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        status = data.get("status")
        if status == "013":  # 조회 결과 없음
            break
        if status != "000":
            raise RuntimeError(f"API 오류 [{status}] {data.get('message')}")

        all_rows.extend(data.get("list", []))
        if page_no >= int(data.get("total_page", 1)):
            break
        page_no += 1

    return pd.DataFrame(all_rows)


@st.cache_data(ttl=600, show_spinner=False)
def check_api_key(api_key: str) -> tuple[str, str]:
    """최소 호출 한 번으로 키 상태(status, message)를 확인. 10분간 캐시."""
    today_s = date.today().strftime("%Y%m%d")
    try:
        resp = requests.get(
            LIST_URL,
            params={
                "crtfc_key": api_key,
                "bgn_de": today_s,
                "end_de": today_s,
                "page_count": 1,
            },
            timeout=10,
        )
        data = resp.json()
        return data.get("status", "?"), data.get("message", "")
    except Exception as e:  # noqa: BLE001
        return "ERR", str(e)


# ──────────────────────────────────────────────────────────
# 관심종목 파일 저장/로드
# ──────────────────────────────────────────────────────────
def get_secret_api_key() -> str:
    """배포 환경(Streamlit Cloud)의 Secrets에 설정된 키. 없으면 빈 문자열."""
    try:
        return st.secrets.get("DART_API_KEY", "")
    except Exception:  # 로컬에 secrets.toml이 없는 경우 등  # noqa: BLE001
        return ""


def load_api_key() -> str:
    """저장된 키 → 환경변수 순으로 불러온다."""
    if API_KEY_FILE.exists():
        return API_KEY_FILE.read_text(encoding="utf-8").strip()
    return os.environ.get("DART_API_KEY", "")


def save_api_key(key: str) -> None:
    API_KEY_FILE.write_text(key.strip(), encoding="utf-8")


def load_watchlist() -> str:
    if WATCHLIST_FILE.exists():
        return WATCHLIST_FILE.read_text(encoding="utf-8").strip()
    return DEFAULT_WATCHLIST


def save_watchlist(text: str) -> None:
    WATCHLIST_FILE.write_text(text.strip(), encoding="utf-8")


def parse_stock_codes(text: str) -> list[str]:
    """쉼표/공백/줄바꿈으로 구분된 6자리 종목코드를 추출 (입력 순서 유지, 중복 제거)."""
    codes = re.findall(r"\b\d{6}\b", text)
    return list(dict.fromkeys(codes))


# ──────────────────────────────────────────────────────────
# 화면 렌더링
# ──────────────────────────────────────────────────────────
def render_disclosure_table(df: pd.DataFrame, show_company: bool = False) -> None:
    """공시 목록 테이블 출력. 보고서명 클릭 시 DART 원문으로 이동.

    LinkColumn은 URL만 받으므로, URL 뒤에 #보고서명을 붙이고
    display_text 정규식으로 이름 부분만 표시한다.
    """
    df = df.sort_values("rcept_dt", ascending=False)
    df["공시일"] = pd.to_datetime(df["rcept_dt"]).dt.date
    df["보고서명"] = VIEWER_URL + df["rcept_no"] + "#" + df["report_nm"]

    cols = ["공시일"] + (["corp_name"] if show_company else []) + ["보고서명", "flr_nm", "rm"]
    view = df[cols].rename(columns={"corp_name": "회사명", "flr_nm": "제출인", "rm": "비고"})

    st.dataframe(
        view,
        use_container_width=True,
        hide_index=True,
        column_config={
            "보고서명": st.column_config.LinkColumn(
                "보고서명", display_text=r"#(.*)", width="large"
            ),
        },
    )


def render_company_tab(api_key: str, bgn: date, end: date, pblntf_ty: str | None) -> None:
    """탭 1: 회사명으로 단일 종목 공시 조회."""
    col_input, col_btn = st.columns([4, 1], vertical_alignment="bottom")
    company = col_input.text_input("회사명", value=DEFAULT_COMPANY, key="company_name")
    if col_btn.button("조회", type="primary", key="btn_company", use_container_width=True):
        st.session_state["company_searched"] = True
    if not st.session_state.get("company_searched"):
        st.info("회사명을 입력하고 **조회** 버튼을 누르세요.")
        return

    matches, similar = find_corp_by_name(company)
    if matches.empty:
        st.error(f"'{company}'와(과) 정확히 일치하는 회사가 없습니다.")
        if not similar.empty:
            st.caption("유사한 회사명: " + ", ".join(similar["corp_name"].head(10)))
        return

    if len(matches) > 1:
        # 정확히 같은 이름의 법인이 여러 개인 경우(상장사/비상장사 동명)만 선택
        options = {
            f"{r.corp_name} ({r.stock_code or '비상장'} · {r.market})": r.corp_code
            for r in matches.itertuples()
        }
        picked = st.selectbox("동일한 이름의 법인이 여러 개입니다. 선택하세요:", options)
        corp_code = options[picked]
        corp_row = matches[matches["corp_code"] == corp_code].iloc[0]
    else:
        corp_row = matches.iloc[0]
        corp_code = corp_row["corp_code"]

    st.subheader(
        f"{corp_row['corp_name']}"
        + (f" · 종목코드 {corp_row['stock_code']}" if corp_row["stock_code"] else " · 비상장")
        + (f" · {corp_row['market']}" if corp_row["market"] else "")
    )
    st.caption(f"DART 고유번호(corp_code): {corp_code}")

    try:
        df = fetch_disclosures(
            api_key, corp_code, bgn.strftime("%Y%m%d"), end.strftime("%Y%m%d"), pblntf_ty
        )
    except RuntimeError as e:
        st.error(str(e))
        return

    if df.empty:
        st.warning("해당 기간에 공시가 없습니다.")
        return

    col1, col2 = st.columns(2)
    col1.metric("공시 건수", f"{len(df):,}건")
    col2.metric("조회 기간", f"{bgn} ~ {end}")
    render_disclosure_table(df)

    with st.expander("📊 월별 공시 건수"):
        monthly = (
            df.assign(month=pd.to_datetime(df["rcept_dt"]).dt.to_period("M").astype(str))
            .groupby("month")
            .size()
        )
        st.bar_chart(monthly)


def render_watchlist_tab(
    api_key: str, bgn: date, end: date, pblntf_ty: str | None, persist: bool = True
) -> None:
    """탭 2: 관심종목 — 종목코드 여러 개를 등록해 공시를 한 번에 조회.

    persist=False(배포 환경)면 방문자별 세션에만 유지하고 서버 파일에 쓰지 않는다.
    """
    if "watchlist_text" not in st.session_state:
        st.session_state["watchlist_text"] = load_watchlist() if persist else DEFAULT_WATCHLIST
    text = st.text_area(
        "종목코드 목록 (쉼표·공백·줄바꿈으로 구분, 6자리)",
        height=100,
        key="watchlist_text",
        on_change=(lambda: save_watchlist(st.session_state["watchlist_text"])) if persist else None,
        help="예: 089970, 005930, 000660"
        + (" — 입력하면 자동 저장되어 다음 실행 때도 유지됩니다." if persist else ""),
    )
    if st.button("관심종목 공시 조회", type="primary", key="btn_watchlist"):
        if persist:
            save_watchlist(text)
        st.session_state["watchlist_searched"] = True
    if not st.session_state.get("watchlist_searched"):
        st.info("종목코드를 입력하고 **관심종목 공시 조회** 버튼을 누르세요.")
        return

    codes = parse_stock_codes(text)
    if not codes:
        st.error("유효한 6자리 종목코드가 없습니다.")
        return

    frames, resolved, failed = [], [], []
    progress = st.progress(0.0, text="조회 중...")
    for i, code in enumerate(codes):
        corp = find_corp_by_stock_code(code)
        if corp is None:
            failed.append(code)
        else:
            resolved.append(f"{corp['corp_name']}({code})")
            try:
                df = fetch_disclosures(
                    api_key,
                    corp["corp_code"],
                    bgn.strftime("%Y%m%d"),
                    end.strftime("%Y%m%d"),
                    pblntf_ty,
                )
            except RuntimeError as e:
                st.error(f"{corp['corp_name']}({code}) 조회 실패: {e}")
                continue
            if not df.empty:
                frames.append(df)
        progress.progress((i + 1) / len(codes), text=f"조회 중... ({i + 1}/{len(codes)})")
    progress.empty()

    if failed:
        st.warning("종목코드를 찾을 수 없음: " + ", ".join(failed))
    if resolved:
        st.caption("조회 대상: " + ", ".join(resolved))

    if not frames:
        st.warning("해당 기간에 공시가 없습니다.")
        return

    merged = pd.concat(frames, ignore_index=True)

    st.metric("조회 기간", f"{bgn} ~ {end}")

    # 회사별 필터
    companies = ["전체"] + sorted(merged["corp_name"].unique())
    picked = st.selectbox("회사 필터", companies, key="watchlist_filter")
    if picked != "전체":
        merged = merged[merged["corp_name"] == picked]

    render_disclosure_table(merged, show_company=True)


def main():
    st.set_page_config(page_title="DART 공시 조회", page_icon="📋", layout="wide")
    st.title("📋 DART 공시 조회")

    # 배포 환경이면 Secrets의 키를 사용하고 입력란을 숨긴다
    secret_key = get_secret_api_key()
    deployed = bool(secret_key)

    # ── 사이드바: 공통 설정 ─────────────────────────────────
    with st.sidebar:
        st.header("설정")
        if deployed:
            api_key = secret_key
        else:
            if "api_key" not in st.session_state:
                st.session_state["api_key"] = load_api_key()
            api_key = st.text_input(
                "OpenDART API 키",
                type="password",
                key="api_key",
                on_change=lambda: save_api_key(st.session_state["api_key"]),
                help="https://opendart.fss.or.kr 에서 발급한 인증키. "
                "입력하면 자동 저장되어 다음 실행 때도 유지됩니다.",
            )
        if api_key:
            status, msg = check_api_key(api_key)
            if status in ("000", "013"):  # 013(결과 없음)도 키 자체는 유효
                st.success("API 키 정상", icon="✅")
            elif status == "011":
                st.error("사용할 수 없는 키 — 유효기간 만료 여부를 확인하세요", icon="❌")
            elif status == "010":
                st.error("등록되지 않은 키", icon="❌")
            elif status == "020":
                st.warning(f"요청 한도 초과: {msg}", icon="⚠️")
            elif status == "ERR":
                st.warning(f"키 상태 확인 실패: {msg}", icon="⚠️")
            else:
                st.error(f"[{status}] {msg}", icon="❌")
        today = date.today()
        dates = st.date_input(
            "조회 기간",
            value=(today - timedelta(days=7), today),
            max_value=today,
        )
        ty_label = st.selectbox("공시 유형", list(PBLNTF_TY.keys()))

    if not api_key:
        st.info("사이드바에 OpenDART API 키를 입력하세요.")
        st.stop()
    # 종료일을 선택하지 않으면 오늘 날짜로 처리
    if isinstance(dates, tuple):
        bgn = dates[0]
        end = dates[1] if len(dates) > 1 else today
    else:
        bgn, end = dates, today
    pblntf_ty = PBLNTF_TY[ty_label]

    tab_watch, tab_company = st.tabs(["⭐ 관심종목", "🔍 회사명 검색"])
    with tab_watch:
        render_watchlist_tab(api_key, bgn, end, pblntf_ty, persist=not deployed)
    with tab_company:
        render_company_tab(api_key, bgn, end, pblntf_ty)


if __name__ == "__main__":
    main()
