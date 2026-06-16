# -*- coding: utf-8 -*-
"""DART 공시 조회 Streamlit 앱 (OpenDART API 사용)"""

import io
import json
import os
import re
from datetime import date, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree as ET

import pandas as pd
import requests
import streamlit as st

CORP_SEARCH_URL = "https://dart.fss.or.kr/dsae001/search.ax"
GNEWS_RSS_URL = "https://news.google.com/rss/search"
LIST_URL = "https://opendart.fss.or.kr/api/list.json"
VIEWER_URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo="

DEFAULT_COMPANY = "브이엠"
WATCHLIST_FILE = Path(__file__).parent / "watchlist.txt"
DEFAULT_WATCHLIST = "089970"
API_KEY_FILE = Path(__file__).parent / ".dart_api_key"

PORTFOLIO_KEY_FILE = Path(r"C:\Users\leegh\Downloads\sample-397404-2ea3826fdcf4.json")
PORTFOLIO_SHEET_URL = "https://docs.google.com/spreadsheets/d/1BwqTOfamzHaSLpcgPrLKspmI6hqPWt9KElralsIQY0M/edit?gid=2145326774#gid=21453267744"

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


def load_portfolio_key() -> dict:
    """서비스 계정 키 로드: 배포 환경은 Streamlit Secrets, 로컬은 JSON 파일."""
    try:
        # Streamlit Cloud: Secrets에 GCP_KEY = '{ ... }' 형태로 저장
        return json.loads(st.secrets["GCP_KEY"])
    except Exception:
        # 로컬: 하드코딩된 파일 경로
        return json.loads(PORTFOLIO_KEY_FILE.read_text(encoding="utf-8"))


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


def _gspread_client(key_dict: dict):
    """서비스 계정 키로 gspread 클라이언트 생성."""
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        raise RuntimeError("gspread 패키지가 설치되지 않았습니다. `pip install gspread`를 실행하세요.")
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(key_dict, scopes=scopes)
    return gspread.authorize(creds)


def load_codes_from_gsheet(key_dict: dict, sheet_url: str, worksheet: str) -> list[str]:
    """구글 스프레드시트에서 6자리 종목코드를 전부 추출해 반환."""
    client = _gspread_client(key_dict)
    sh = client.open_by_url(sheet_url)
    ws = sh.worksheet(worksheet) if worksheet else sh.get_worksheet(0)
    all_values = ws.get_all_values()

    codes = []
    for row in all_values:
        for cell in row:
            if re.match(r"^\d{6}$", cell.strip()):
                codes.append(cell.strip())
    return list(dict.fromkeys(codes))


def load_portfolio_from_gsheet(key_dict: dict, sheet_url: str) -> pd.DataFrame:
    """구글 시트 '포트폴리오 요약'에서 5개 컬럼을 로드."""
    client = _gspread_client(key_dict)
    sh = client.open_by_url(sheet_url)
    ws = sh.worksheet("포트폴리오 요약")
    records = ws.get_all_records()
    df = pd.DataFrame(records)

    cols = ["종목명", "종목코드", "수량", "현재가격", "평가금액", "매수가격"]
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise ValueError(f"시트에 다음 컬럼이 없습니다: {missing}")

    df = df[cols].copy()
    df["종목코드"] = df["종목코드"].astype(str).apply(
        lambda x: x.strip().zfill(6) if x.strip().isdigit() else x.strip()
    )
    for col in ["수량", "현재가격", "평가금액", "매수가격"]:
        df[col] = pd.to_numeric(
            df[col].astype(str).str.replace(",", "").str.replace(" ", ""),
            errors="coerce",
        )
    return df.dropna(subset=["종목코드"]).reset_index(drop=True)


def _period_base_date(period: str) -> tuple[date, date]:
    """(기준일, 다운로드 시작일) 반환. 기준일 종가를 1로 정규화."""
    today = date.today()
    if period == "DTD":
        base = today - timedelta(days=1)
    elif period == "WTD":
        days_to_friday = (today.weekday() - 4) % 7 or 7  # 직전 금요일 (당일 금요일이면 1주 전)
        base = today - timedelta(days=days_to_friday)
    elif period == "MTD":
        base = date(today.year, today.month, 1) - timedelta(days=1)
    elif period == "QTD":
        q_start_month = ((today.month - 1) // 3) * 3 + 1
        base = date(today.year, q_start_month, 1) - timedelta(days=1)
    else:  # YTD
        base = date(today.year - 1, 12, 31)
    return base, base - timedelta(days=7)


@st.cache_data(ttl=300, show_spinner="주가 데이터 로딩 중...")
def fetch_portfolio_prices(codes: tuple[str, ...], download_from: str) -> pd.DataFrame:
    """종목코드별 일별 종가를 yfinance로 조회 (KOSPI → KOSDAQ 순으로 시도)."""
    import yfinance as yf

    frames: dict[str, pd.Series] = {}
    for code in codes:
        for suffix in [".KS", ".KQ"]:
            data = yf.download(
                f"{code}{suffix}",
                start=download_from,
                progress=False,
                auto_adjust=True,
            )
            if data.empty:
                continue
            close = data["Close"]
            if isinstance(close, pd.DataFrame):
                close = close.iloc[:, 0]
            frames[code] = close
            break

    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames, axis=1)
    combined.index = pd.to_datetime(combined.index).normalize()
    return combined


@st.cache_data(ttl=300, show_spinner="미국 주가 로딩 중...")
def fetch_us_prices(tickers: tuple[str, ...], download_from: str) -> pd.DataFrame:
    """미국 종목 티커로 일별 종가 조회."""
    import yfinance as yf

    frames: dict[str, pd.Series] = {}
    for ticker in tickers:
        data = yf.download(ticker, start=download_from, progress=False, auto_adjust=True)
        if data.empty:
            continue
        close = data["Close"]
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]
        frames[ticker] = close

    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames, axis=1)
    combined.index = pd.to_datetime(combined.index).normalize()
    return combined


@st.cache_data(ttl=300, show_spinner=False)
def fetch_us_index_prices(download_from: str) -> pd.DataFrame:
    """S&P500(^GSPC)과 NASDAQ(^IXIC) 일별 종가 조회."""
    import yfinance as yf

    frames: dict[str, pd.Series] = {}
    for name, ticker in [("S&P500", "^GSPC"), ("NASDAQ", "^IXIC")]:
        data = yf.download(ticker, start=download_from, progress=False, auto_adjust=True)
        if data.empty:
            continue
        close = data["Close"]
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]
        frames[name] = close

    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames, axis=1)
    combined.index = pd.to_datetime(combined.index).normalize()
    return combined


@st.cache_data(ttl=300, show_spinner=False)
def fetch_index_prices(download_from: str) -> pd.DataFrame:
    """KOSPI(^KS11)와 KOSDAQ(^KQ11) 일별 종가 조회."""
    import yfinance as yf

    frames: dict[str, pd.Series] = {}
    for name, ticker in [("KOSPI", "^KS11"), ("KOSDAQ", "^KQ11")]:
        data = yf.download(ticker, start=download_from, progress=False, auto_adjust=True)
        if data.empty:
            continue
        close = data["Close"]
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]
        frames[name] = close

    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames, axis=1)
    combined.index = pd.to_datetime(combined.index).normalize()
    return combined


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


def render_portfolio_tab() -> None:
    """탭: 포트폴리오 원 그래프 + 수익률 비교 선그래프."""
    import plotly.express as px
    import plotly.graph_objects as go

    # ── 자동 로드 ───────────────────────────────────────────
    if "portfolio_df" not in st.session_state:
        try:
            key_dict = load_portfolio_key()
            df = load_portfolio_from_gsheet(key_dict, PORTFOLIO_SHEET_URL)
            st.session_state["portfolio_df"] = df
        except Exception as e:
            st.error(f"포트폴리오 로드 실패: {e}")
            return

    portfolio_df: pd.DataFrame | None = st.session_state.get("portfolio_df")
    if portfolio_df is None or portfolio_df.empty:
        return

    # ── 상단: 원 그래프 | 종목별 수익률 표 ────────────────
    col_pie, col_table = st.columns([1, 1])

    with col_pie:
        st.subheader("평가금액 비중")
        fig_pie = px.pie(
            portfolio_df,
            values="평가금액",
            names="종목명",
            hole=0.35,
        )
        fig_pie.update_traces(
            textposition="inside",
            textinfo="percent+label",
            hovertemplate="<b>%{label}</b><br>%{percent}<extra></extra>",
        )
        fig_pie.update_layout(showlegend=False, margin=dict(t=10, b=0, l=0, r=0))
        st.plotly_chart(fig_pie, use_container_width=True)

    with col_table:
        st.subheader("종목별 수익률")
        tbl = portfolio_df[["종목명", "매수가격", "현재가격", "평가금액"]].copy()
        tbl["수익률(%)"] = (tbl["현재가격"] / tbl["매수가격"] - 1) * 100
        tbl = tbl.sort_values("평가금액", ascending=False).reset_index(drop=True)
        tbl = tbl.rename(columns={"매수가격": "매수금액"})

        def _color_return(v):
            if v > 0:
                return "color: #39FF14; font-weight: bold"
            elif v < 0:
                return "color: #FF3333; font-weight: bold"
            return ""

        _styler = tbl[["종목명", "매수금액", "현재가격", "수익률(%)"]].style
        _style_fn = getattr(_styler, "map", None) or getattr(_styler, "applymap")
        styled = _style_fn(_color_return, subset=["수익률(%)"]).format(
            {"매수금액": "{:,.0f}", "현재가격": "{:,.0f}", "수익률(%)": "{:+.2f}%"}
        )
        st.dataframe(styled, use_container_width=True, hide_index=True)

    # ── 하단: 수익률 비교 선그래프 ────────────────────────
    today = date.today()
    base_date = date(today.year - 1, 12, 31)
    base_date_str = base_date.strftime("%Y/%m/%d")
    download_from = base_date - timedelta(days=7)
    base_ts = pd.Timestamp(base_date)

    palette = [
        "#E63946", "#2196F3", "#4CAF50", "#FF9800", "#9C27B0",
        "#00BCD4", "#FF5722", "#8BC34A", "#F06292", "#FFD600",
    ]

    def _draw_return_chart(price_df, keys, name_map, idx_df, idx_styles):
        if price_df.empty:
            st.warning("주가 데이터를 가져올 수 없습니다.")
            return
        past = price_df[price_df.index <= base_ts]
        base_prices = past.iloc[-1] if not past.empty else price_df.iloc[0]
        chart_df = price_df[price_df.index >= base_ts]
        if chart_df.empty:
            st.warning("차트 데이터가 없습니다.")
            return

        fig = go.Figure()
        for i, key in enumerate(keys):
            if key not in chart_df.columns:
                continue
            name = name_map.get(key, key)
            color = palette[i % len(palette)]
            base_val = float(base_prices[key]) if pd.notna(base_prices.get(key, None)) else None
            if not base_val:
                continue
            fig.add_trace(go.Scatter(
                x=chart_df.index, y=chart_df[key] / base_val,
                mode="lines", name=name,
                line=dict(color=color, width=2.5),
                hovertemplate=f"{name}: %{{y:.4f}}<extra></extra>",
                legend="legend",
            ))

        for idx_name, style in idx_styles.items():
            if idx_name not in idx_df.columns:
                continue
            idx_past = idx_df[idx_df.index <= base_ts]
            idx_base = float(idx_past[idx_name].iloc[-1]) if not idx_past.empty else float(idx_df[idx_name].iloc[0])
            if not idx_base:
                continue
            idx_chart = idx_df[idx_df.index >= base_ts]
            fig.add_trace(go.Scatter(
                x=idx_chart.index, y=idx_chart[idx_name] / idx_base,
                mode="lines", name=idx_name, line=style,
                hovertemplate=f"{idx_name}: %{{y:.4f}}<extra></extra>",
                legend="legend2",
            ))

        fig.add_hline(y=1.0, line_dash="dash", line_color="gray", opacity=0.4)
        fig.update_layout(
            yaxis_title="정규화 수익률 (기준=1)", xaxis_title="",
            hovermode="x unified",
            legend=dict(title="종목", orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
            legend2=dict(title="지수", orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            margin=dict(t=60, b=0, l=0, r=0),
        )
        fig.update_xaxes(range=[pd.Timestamp(base_date), chart_df.index[-1]])
        st.plotly_chart(fig, use_container_width=True)

    # 한국/미국 종목 분리
    kr_df = portfolio_df[portfolio_df["종목코드"].str.isdigit()]
    us_df = portfolio_df[~portfolio_df["종목코드"].str.isdigit()].copy()

    # ── 한국 주식 ──────────────────────────────────────────
    if not kr_df.empty:
        kr_codes = tuple(kr_df["종목코드"].tolist())
        kr_name_map = dict(zip(kr_df["종목코드"], kr_df["종목명"]))
        st.subheader(f"한국 주식 수익률 비교 (기준일: {base_date_str})")
        _draw_return_chart(
            fetch_portfolio_prices(kr_codes, download_from.strftime("%Y-%m-%d")),
            kr_codes, kr_name_map,
            fetch_index_prices(download_from.strftime("%Y-%m-%d")),
            {"KOSPI": dict(color="#CC00FF", width=3, dash="solid"),
             "KOSDAQ": dict(color="#39FF14", width=3, dash="solid")},
        )

    # ── 미국 주식 ──────────────────────────────────────────
    if not us_df.empty:
        us_df["ticker"] = us_df["종목코드"]
        us_tickers = tuple(us_df["ticker"].tolist())
        us_name_map = dict(zip(us_df["ticker"], us_df["종목명"]))
        st.subheader(f"미국 주식 수익률 비교 (기준일: {base_date_str})")
        _draw_return_chart(
            fetch_us_prices(us_tickers, download_from.strftime("%Y-%m-%d")),
            us_tickers, us_name_map,
            fetch_us_index_prices(download_from.strftime("%Y-%m-%d")),
            {"S&P500": dict(color="#CC00FF", width=3, dash="solid"),
             "NASDAQ": dict(color="#39FF14", width=3, dash="solid")},
        )


def render_watchlist_tab(
    api_key: str, bgn: date, end: date, pblntf_ty: str | None, persist: bool = True
) -> None:
    """탭 2: 관심종목 — 종목코드 여러 개를 등록해 공시를 한 번에 조회.

    persist=False(배포 환경)면 방문자별 세션에만 유지하고 서버 파일에 쓰지 않는다.
    """
    if "watchlist_text" not in st.session_state:
        st.session_state["watchlist_text"] = load_watchlist() if persist else DEFAULT_WATCHLIST

    with st.expander("📊 구글 스프레드시트에서 불러오기"):
        key_file = st.file_uploader(
            "서비스 계정 JSON 키 파일",
            type="json",
            help="Google Cloud Console에서 발급한 서비스 계정 키(.json)를 업로드하세요.",
            key="gsheet_key_file",
        )
        sheet_url = st.text_input(
            "스프레드시트 URL",
            placeholder="https://docs.google.com/spreadsheets/d/...",
            key="gsheet_url",
        )
        worksheet_name = st.text_input(
            "시트 이름 (비우면 첫 번째 시트)",
            placeholder="Sheet1",
            key="gsheet_worksheet",
        )
        if st.button("시트에서 종목코드 불러오기", key="btn_gsheet"):
            if not key_file:
                st.error("서비스 계정 JSON 파일을 업로드하세요.")
            elif not sheet_url.strip():
                st.error("스프레드시트 URL을 입력하세요.")
            else:
                try:
                    key_dict = json.load(key_file)
                    codes = load_codes_from_gsheet(key_dict, sheet_url.strip(), worksheet_name.strip())
                    if not codes:
                        st.warning("시트에서 6자리 종목코드를 찾지 못했습니다.")
                    else:
                        imported = "\n".join(codes)
                        st.session_state["watchlist_text"] = imported
                        if persist:
                            save_watchlist(imported)
                        st.success(f"{len(codes)}개 종목코드를 불러왔습니다: {', '.join(codes[:5])}" + (" ..." if len(codes) > 5 else ""))
                        st.rerun()
                except Exception as e:
                    st.error(f"불러오기 실패: {e}")

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


@st.cache_data(ttl=600, show_spinner=False)
def fetch_news(company_name: str) -> pd.DataFrame:
    """구글 뉴스 RSS에서 종목명으로 기사를 검색해 DataFrame으로 반환."""
    try:
        resp = requests.get(
            GNEWS_RSS_URL,
            params={"q": company_name, "hl": "ko", "gl": "KR", "ceid": "KR:ko"},
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception:
        return pd.DataFrame()

    rows = []
    for item in root.findall(".//item"):
        title = item.findtext("title", "").strip()
        link = item.findtext("link", "").strip()
        pub_date_str = item.findtext("pubDate", "")
        source_el = item.find("source")
        source = source_el.text.strip() if source_el is not None and source_el.text else ""

        try:
            pub_date = parsedate_to_datetime(pub_date_str).date()
        except Exception:
            pub_date = None

        if title and link:
            rows.append({"날짜": pub_date, "종목명": company_name, "제목": title, "링크": link, "언론사": source})

    return pd.DataFrame(rows)


def render_news_tab() -> None:
    """포트폴리오 종목들의 구글 뉴스를 조회해 표시."""
    portfolio_df = st.session_state.get("portfolio_df")
    if portfolio_df is None or portfolio_df.empty:
        st.info("먼저 **📈 포트폴리오** 메뉴에서 구글 시트를 불러오세요.")
        return

    names = portfolio_df["종목명"].tolist()
    today = date.today()

    with st.sidebar:
        st.divider()
        st.header("설정")
        dates = st.date_input(
            "조회 기간",
            value=(today - timedelta(days=7), today),
            max_value=today,
            key="news_date_range",
        )
        picked = st.selectbox("종목 필터", ["전체"] + names, key="news_company_filter")
        if st.button("🔄 새로고침", key="btn_news_reload", use_container_width=True):
            fetch_news.clear()
            st.rerun()

    if isinstance(dates, tuple) and len(dates) == 2:
        bgn_news, end_news = dates
    else:
        bgn_news, end_news = today - timedelta(days=7), today

    target_names = names if picked == "전체" else [picked]

    frames = []
    progress = st.progress(0.0, text="뉴스 검색 중...")
    for i, name in enumerate(target_names):
        try:
            df = fetch_news(name)
            if not df.empty:
                frames.append(df)
        except Exception as e:
            st.warning(f"{name} 뉴스 검색 실패: {e}")
        progress.progress((i + 1) / len(target_names), text=f"뉴스 검색 중... ({i + 1}/{len(target_names)})")
    progress.empty()

    if not frames:
        st.warning("검색된 뉴스가 없습니다.")
        return

    merged = pd.concat(frames, ignore_index=True)
    merged = merged.dropna(subset=["날짜"])
    merged = merged[(merged["날짜"] >= bgn_news) & (merged["날짜"] <= end_news)]
    merged = merged.sort_values("날짜", ascending=False).reset_index(drop=True)

    if merged.empty:
        st.warning(f"{bgn_news} ~ {end_news} 기간에 검색된 뉴스가 없습니다.")
        return

    st.metric("뉴스 건수", f"{len(merged):,}건")

    merged["제목_col"] = merged["링크"] + "#" + merged["제목"]
    view = merged[["날짜", "종목명", "제목_col", "언론사"]].rename(columns={"제목_col": "제목"})

    st.dataframe(
        view,
        use_container_width=True,
        hide_index=True,
        column_config={
            "제목": st.column_config.LinkColumn("제목", display_text=r"#(.*)", width="large"),
        },
    )


def render_dart_portfolio_tab(
    api_key: str, bgn: date, end: date, pblntf_ty: str | None
) -> None:
    """포트폴리오 종목들의 DART 공시를 자동 조회."""
    portfolio_df = st.session_state.get("portfolio_df")
    if portfolio_df is None or portfolio_df.empty:
        st.info("먼저 **📈 포트폴리오** 메뉴에서 구글 시트를 불러오세요.")
        return

    codes = portfolio_df["종목코드"].tolist()
    frames, resolved = [], []

    progress = st.progress(0.0, text="공시 조회 중...")
    for i, code in enumerate(codes):
        corp = find_corp_by_stock_code(code)
        if corp is not None:
            resolved.append(f"{corp['corp_name']}({code})")
            try:
                df = fetch_disclosures(
                    api_key,
                    corp["corp_code"],
                    bgn.strftime("%Y%m%d"),
                    end.strftime("%Y%m%d"),
                    pblntf_ty,
                )
                if not df.empty:
                    frames.append(df)
            except RuntimeError:
                pass  # DART에 없는 종목(미국주 등) 조용히 스킵
        progress.progress((i + 1) / len(codes), text=f"공시 조회 중... ({i + 1}/{len(codes)})")
    progress.empty()

    if resolved:
        st.caption("조회 대상: " + ", ".join(resolved))

    if not frames:
        st.warning("해당 기간에 공시가 없습니다.")
        return

    merged = pd.concat(frames, ignore_index=True)
    st.metric("조회 기간", f"{bgn} ~ {end}")

    companies = ["전체"] + sorted(merged["corp_name"].unique())
    picked = st.selectbox("회사 필터", companies, key="dart_pf_filter")
    if picked != "전체":
        merged = merged[merged["corp_name"] == picked]

    render_disclosure_table(merged, show_company=True)


def main():
    st.set_page_config(page_title="DART 공시 조회", page_icon="📋", layout="wide")

    # ── 사이드바: 메뉴 ──────────────────────────────────────
    if "page" not in st.session_state:
        st.session_state["page"] = "📈 포트폴리오"

    with st.sidebar:
        if st.button("📈 포트폴리오", use_container_width=True,
                     type="primary" if st.session_state["page"] == "📈 포트폴리오" else "secondary"):
            st.session_state["page"] = "📈 포트폴리오"
            st.rerun()
        if st.button("📋 DART 공시", use_container_width=True,
                     type="primary" if st.session_state["page"] == "📋 DART 공시" else "secondary"):
            st.session_state["page"] = "📋 DART 공시"
            st.rerun()
        if st.button("📰 종목 뉴스", use_container_width=True,
                     type="primary" if st.session_state["page"] == "📰 종목 뉴스" else "secondary"):
            st.session_state["page"] = "📰 종목 뉴스"
            st.rerun()

    page = st.session_state["page"]

    # ── 포트폴리오 페이지 ───────────────────────────────────
    if page == "📈 포트폴리오":
        with st.sidebar:
            st.divider()
            if st.button("🔄 새로고침", key="btn_pf_reload", use_container_width=True):
                try:
                    key_dict = load_portfolio_key()
                    df = load_portfolio_from_gsheet(key_dict, PORTFOLIO_SHEET_URL)
                    st.session_state["portfolio_df"] = df
                    st.rerun()
                except Exception as e:
                    st.error(f"새로고침 실패: {e}")
        st.title("📈 포트폴리오")
        render_portfolio_tab()
        return

    # ── 뉴스 페이지 ─────────────────────────────────────────
    if page == "📰 종목 뉴스":
        st.title("📰 종목 뉴스")
        render_news_tab()
        return

    # ── DART 공시 페이지: 사이드바 설정 ────────────────────
    st.title("📋 DART 공시")

    secret_key = get_secret_api_key()
    deployed = bool(secret_key)

    with st.sidebar:
        st.divider()
        st.header("설정")
        if deployed:
            api_key = secret_key
        else:
            if "api_key" not in st.session_state:
                st.session_state["api_key"] = load_api_key()
            api_key = st.text_input(
                "DART API 키",
                key="api_key",
                on_change=lambda: save_api_key(st.session_state["api_key"]),
                help="https://opendart.fss.or.kr 에서 발급한 인증키.",
            )
        if api_key:
            status, msg = check_api_key(api_key)
            if status in ("000", "013"):
                st.success("API 키 정상", icon="✅")
            elif status == "011":
                st.error("유효기간 만료", icon="❌")
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
        if st.button("🔄 새로고침", key="btn_dart_reload", use_container_width=True):
            fetch_disclosures.clear()
            st.rerun()

    if not api_key:
        st.info("사이드바에 OpenDART API 키를 입력하세요.")
        st.stop()

    if isinstance(dates, tuple):
        bgn = dates[0]
        end = dates[1] if len(dates) > 1 else today
    else:
        bgn, end = dates, today
    pblntf_ty = PBLNTF_TY[ty_label]

    render_dart_portfolio_tab(api_key, bgn, end, pblntf_ty)


if __name__ == "__main__":
    main()
