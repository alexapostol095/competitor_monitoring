"""

- Prices can be shown in EUR (converted) or Local currency via a sidebar toggle.
  The original Currency column travels with every detail view either way.
- Benchmark is RRP only. PriceIndex = Price / RRP is unitless (currency cancels),
  so it compares cleanly across countries in both modes. No RRP -> no index.
- Production data is daily; current views collapse to the LATEST observation per
  shop x product x country first. History keeps all rows.
"""
import re
import html
import pandas as pd
import streamlit as st

st.set_page_config(page_title="Competitor Pricing Dashboard", layout="wide")

REQUIRED = ["PriceDate", "CountryCode", "ShopName", "SymsonProductId", "ProductName",
            "Brand", "MainGroup", "SubGroup", "Price", "TotalCost", "Currency", "RRP"]


# ----------------------------- data layer (pure) -----------------------------
@st.cache_data(show_spinner=False)
def load_data(file) -> pd.DataFrame:
    df = pd.read_csv(file)
    df["PriceDate"] = pd.to_datetime(df["PriceDate"], errors="coerce")
    df["ShopName"] = df["ShopName"].astype(str).map(html.unescape).str.strip()
    if "ProductName" in df:
        df["ProductName"] = df["ProductName"].fillna(df["SymsonProductId"])
    for c in ["Brand", "MainGroup", "SubGroup"]:
        if c in df:
            df[c] = df[c].fillna("(missing)")
    return df


def latest_snapshot(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    idx = df.groupby(["SymsonProductId", "ShopName", "CountryCode"])["PriceDate"].idxmax()
    return df.loc[idx]


def price_index(df: pd.DataFrame) -> pd.Series:
    """Price as a fraction of RRP. Unitless, currency-invariant. Blank if no RRP."""
    rrp = pd.to_numeric(df["RRP"], errors="coerce")
    return df["Price"] / rrp.where(rrp > 0)


def cols_for_mode(df: pd.DataFrame, eur: bool) -> dict:
    """Resolve which columns to read for the chosen display currency."""
    have_eur = {"Price_EUR", "TotalCost_EUR", "RRP_EUR"}.issubset(df.columns)
    if eur and have_eur:
        return dict(price="Price_EUR", total="TotalCost_EUR",
                    ship="ShippingCost_EUR", rrp="RRP_EUR", unit="EUR", eur=True)
    return dict(price="Price", total="TotalCost",
                ship="ShippingCost", rrp="RRP", unit=None, eur=False)


def one_currency(df: pd.DataFrame):
    cur = df["Currency"].dropna().unique()
    return cur[0] if len(cur) == 1 else None


def unit_label(cm, df):
    """Human label for the value unit in the current mode."""
    if cm["eur"]:
        return "EUR"
    c = one_currency(df)
    return c if c else "local currency"


# ----------------------------- styling helper --------------------------------
def style_index(mat: pd.DataFrame, dp: int):
    def css(x):
        if pd.isna(x):
            return ""
        t = min(max((x - 0.6) / 0.6, 0), 1)
        r, g, b = int(230 - 110 * t), int(150 + 30 * t), 70
        return f"background-color: rgba({r},{g},{b},0.30)"
    sty = mat.style.format(precision=dp, na_rep="—")
    try:
        return sty.map(css)
    except AttributeError:
        return sty.applymap(css)


# ----------------------------- views -----------------------------------------
def view_overview(snap, cm, value_key, value_label, dp):
    st.subheader("Europe overview")
    value_col = cm[value_key]
    unit = unit_label(cm, snap)

    c = st.columns(5)
    c[0].metric("Products", snap["SymsonProductId"].nunique())
    c[1].metric("Competitors", snap["ShopName"].nunique())
    c[2].metric("Countries", snap["CountryCode"].nunique())
    c[3].metric("RRP present", f"{snap['RRP'].notna().mean():.0%}")
    c[4].metric("Window", f"{snap.PriceDate.min():%d %b} – {snap.PriceDate.max():%d %b}")

    gran = st.selectbox("Rows", ["ProductName", "SubGroup", "MainGroup", "Brand"], index=0)
    metric = st.radio("Cell metric", [f"Cheapest {value_label}", "Median price index (vs RRP)"],
                      horizontal=True)

    cc = st.columns([3, 1])
    multi = cc[0].checkbox("Only products priced in more than one country", value=False)
    min_ctry = int(cc[1].number_input("min countries", 2, 20, 2)) if multi else 2

    d = snap.copy()
    if multi:
        cov = (d.dropna(subset=[value_col])
                .groupby("SymsonProductId")["CountryCode"].nunique())
        keep = cov[cov >= min_ctry].index
        d = d[d["SymsonProductId"].isin(keep)]
        st.caption(f"{len(keep):,} of {snap['SymsonProductId'].nunique():,} products "
                   f"are priced in ≥ {min_ctry} countries.")
        if d.empty:
            st.info("No products meet the multi-country threshold under the current filters "
                    "(a single-country sidebar selection will always empty this).")
            return

    work = d.copy()
    if metric.startswith("Cheapest"):
        d = d.groupby([gran, "CountryCode"])[value_col].min().reset_index()
        mat = d.pivot(index=gran, columns="CountryCode", values=value_col)
        if cm["eur"]:
            mat.insert(0, "Europe", mat.min(axis=1))
            st.caption(f"Cheapest {value_label} per country (EUR). 'Europe' = min across countries.")
        elif one_currency(snap):
            mat.insert(0, "Europe", mat.min(axis=1))
            st.caption(f"Cheapest {value_label} per country ({unit}). "
                       "'Europe' = min across countries (single currency in this selection).")
        else:
            st.caption(f"Cheapest {value_label} per country, local currency — **not converted**, "
                       "so no Europe total. Switch the sidebar to EUR for a cross-country minimum.")
        st.dataframe(mat.style.format(precision=dp, na_rep="—"), use_container_width=True, height=520)
    else:
        d["idx"] = price_index(d)
        d = d.groupby([gran, "CountryCode"])["idx"].median().reset_index()
        mat = d.pivot(index=gran, columns="CountryCode", values="idx")
        mat.insert(0, "Europe", mat.median(axis=1))
        st.caption("Median price as a fraction of RRP (1.0 = at RRP). Unitless, comparable across "
                   "countries. Amber = below RRP. Blank = no RRP for that cell.")
        st.dataframe(style_index(mat, dp), use_container_width=True, height=520)

    _overview_drill(work)


def _overview_drill(work):
    """Jump a chosen product (optionally focused on one country) into the Article view.
    Streamlit has no native click-into-cell, so this is an explicit product+country picker
    that lands the user on the same detail Victor wanted from clicking a price cell."""
    st.divider()
    st.caption("Inspect a product — opens it in the Article view, optionally focused on one country")
    prods = sorted(work["ProductName"].unique())
    cc = st.columns([3, 1, 1])
    prod = cc[0].selectbox("Product", prods, key="drill_prod_pick")
    ctry_opts = ["All countries"] + sorted(work.loc[work.ProductName.eq(prod), "CountryCode"].unique())
    focus = cc[1].selectbox("Country", ctry_opts, key="drill_ctry_pick")
    cc[2].markdown("<div style='height:1.8em'></div>", unsafe_allow_html=True)
    if cc[2].button("Open →", use_container_width=True):
        st.session_state["drill_product"] = prod
        st.session_state["drill_country"] = None if focus == "All countries" else focus
        st.session_state["_go_article"] = True
        st.rerun()


def view_article(snap, cm, value_key, value_label, dp, undercut):
    st.subheader("Article view — one product, cheapest first")
    value_col = cm[value_key]
    unit = unit_label(cm, snap)
    prods = snap[["SymsonProductId", "ProductName"]].drop_duplicates().sort_values("ProductName")
    names = list(prods["ProductName"])

    # a drill-through from the overview pre-selects the product (and maybe a country)
    drill_prod = st.session_state.pop("drill_product", None)
    drill_ctry = st.session_state.pop("drill_country", None)
    idx = names.index(drill_prod) if drill_prod in names else 0
    label = st.selectbox("Product", names, index=idx)
    pid = prods.loc[prods.ProductName.eq(label), "SymsonProductId"].iloc[0]

    d = snap[snap.SymsonProductId.eq(pid)].copy()
    d["Index"] = price_index(d)

    country_opts = ["All countries"] + sorted(d["CountryCode"].unique())
    c_idx = country_opts.index(drill_ctry) if drill_ctry in country_opts else 0
    focus = st.selectbox("Country focus", country_opts, index=c_idx)
    if focus != "All countries":
        d = d[d.CountryCode.eq(focus)]

    cheapest = (d.loc[d.groupby("CountryCode")[value_col].idxmin()]
                 .sort_values(value_col)[["CountryCode", "ShopName", value_col,
                                          cm["rrp"], "Currency", "Index"]])
    st.caption(f"Cheapest {value_label} per country ({unit})")
    st.dataframe(cheapest, use_container_width=True, hide_index=True,
                 column_config={value_col: st.column_config.NumberColumn(format=f"%.{dp}f"),
                                cm["rrp"]: st.column_config.NumberColumn(format=f"%.{dp}f"),
                                "Index": st.column_config.NumberColumn(format=f"%.{dp}f")})

    cap = "All offers, cheapest first" + (f" — {focus} only" if focus != "All countries" else "")
    st.caption(cap)
    cols = [c for c in ["CountryCode", "ShopName", cm["price"], cm["ship"], cm["total"],
                        cm["rrp"], "Currency", "Index"] if c in d.columns]
    full = d.sort_values(value_col)[cols]
    if undercut is not None:
        full = full[full.Index < undercut]
    st.dataframe(full, use_container_width=True, hide_index=True)


def view_competitor(snap, cm, value_key, value_label, dp):
    st.subheader("Competitor view — one competitor across products & countries")
    who = st.selectbox("Competitor", sorted(snap["ShopName"].unique()))

    d = snap[snap["ShopName"].eq(who)].copy()
    d["Index"] = price_index(d)
    c = st.columns(3)
    c[0].metric("Products listed", d.SymsonProductId.nunique())
    c[1].metric("Countries", d.CountryCode.nunique())
    med = d.Index.median()
    c[2].metric("Median index", f"{med:.{dp}f}" if pd.notna(med) else "—")

    cols = [x for x in ["CountryCode", "ProductName", cm["price"], cm["total"],
                        cm["rrp"], "Currency", "Index"] if x in d.columns]
    st.dataframe(d.sort_values("Index")[cols], use_container_width=True, hide_index=True,
                 column_config={"Index": st.column_config.NumberColumn(format=f"%.{dp}f")})


def view_benchmark(snap, dp, undercut):
    st.subheader("Benchmarking — index vs RRP")
    d = snap.copy()
    d["Index"] = price_index(d)
    d = d.dropna(subset=["Index"])
    if d.empty:
        st.info("No rows have an RRP under the current filters, so no index can be computed.")
        return

    cc = st.columns([2, 1])
    gran = cc[0].selectbox("Group by", ["MainGroup", "SubGroup", "Brand"], index=0)
    stat = cc[1].radio("Statistic", ["Median", "Mean"], horizontal=True)
    func = "median" if stat == "Median" else "mean"
    agg = (d.groupby([gran, "CountryCode"])["Index"]
            .agg(func).reset_index().pivot(index=gran, columns="CountryCode", values="Index"))
    st.caption(f"{stat} price index by country (1.0 = at RRP, unitless). "
               + ("Median resists outlier offers; " if func == "median"
                  else "Mean is pulled by extreme offers; ")
               + "blank = no RRP for that cell.")
    st.dataframe(style_index(agg, dp), use_container_width=True)

    thr = undercut if undercut is not None else 0.75
    flagged = d[d.Index < thr]
    st.caption(f"Observations below {thr:.0%} of RRP  ({len(flagged):,} of {len(d):,})")
    if not flagged.empty:
        cols = [c for c in ["CountryCode", "ShopName", "ProductName", "Price",
                            "Currency", "RRP", "Index"] if c in flagged]
        st.dataframe(flagged.sort_values("Index")[cols], use_container_width=True, hide_index=True,
                     column_config={"Index": st.column_config.NumberColumn(format=f"%.{dp}f")})


def view_violators(snap, cm, dp):
    st.subheader("RRP violators — selling above recommended price")
    basis = st.radio("Compare to RRP using", ["Sticker price", "Landed (incl. shipping)"],
                     horizontal=True)
    col = cm["price"] if basis.startswith("Sticker") else cm["total"]
    rrp_col = cm["rrp"]
    unit = unit_label(cm, snap)

    d = snap.copy()
    d[rrp_col] = pd.to_numeric(d[rrp_col], errors="coerce")
    d = d[d[rrp_col] > 0]
    if d.empty:
        st.info("No rows have an RRP under the current filters, so there is nothing to compare.")
        return

    d["Ratio"] = d[col] / d[rrp_col]
    v = d[d[col] > d[rrp_col]].copy()
    v["Over_by"] = (v[col] - v[rrp_col]).round(dp)
    v["Over_%"] = ((v["Ratio"] - 1) * 100).round(1)

    c = st.columns(4)
    c[0].metric("Offers above RRP", f"{len(v):,}")
    c[1].metric("of offers with RRP", f"{len(v) / len(d):.0%}")
    c[2].metric("Products", v["SymsonProductId"].nunique())
    c[3].metric("Competitors", v["ShopName"].nunique())

    if v.empty:
        st.success(f"No competitor exceeds RRP on {basis.lower()} under the current filters.")
        return

    if col == cm["price"]:
        st.caption(f"Shelf price above the country RRP, worst first (values in {unit}). "
                   "Price and RRP share the currency, so this is like-for-like.")
    else:
        st.caption(f"Landed cost (incl. shipping) above RRP, worst first (values in {unit}). "
                   "Shipping is in the landed figure but not in RRP, so some rows exceed RRP on "
                   "shipping alone — check the sticker basis too.")

    cols = [x for x in ["CountryCode", "ShopName", "ProductName", col, rrp_col,
                        "Over_by", "Over_%", "Currency"] if x in v.columns]
    st.dataframe(v.sort_values("Over_%", ascending=False)[cols],
                 use_container_width=True, hide_index=True,
                 column_config={col: st.column_config.NumberColumn(format=f"%.{dp}f"),
                                rrp_col: st.column_config.NumberColumn(format=f"%.{dp}f"),
                                "Over_by": st.column_config.NumberColumn(format=f"%.{dp}f"),
                                "Over_%": st.column_config.NumberColumn(format="%.1f%%")})

    st.caption("Offers above RRP, by country")
    st.bar_chart(v.groupby("CountryCode").size())


def view_history(fdf, cm, value_key, value_label):
    st.subheader("Price history")
    value_col = cm[value_key]
    if fdf.PriceDate.dt.date.nunique() < 3:
        st.info("Only a few distinct dates so far — history fills in as scrapes accumulate.")
    prods = fdf[["SymsonProductId", "ProductName"]].drop_duplicates().sort_values("ProductName")
    label = st.selectbox("Product", prods["ProductName"], key="hist_prod")
    pid = prods.loc[prods.ProductName.eq(label), "SymsonProductId"].iloc[0]
    d = fdf[fdf.SymsonProductId.eq(pid)].copy()
    d["day"] = d.PriceDate.dt.date

    if cm["eur"]:
        note = " (EUR)"
    else:
        cur = one_currency(d)
        note = f" ({cur})" if cur else " — mixed currencies; switch to EUR or filter to one country"

    # cheapest per country per day, and WHICH shop set it (Victor: "who is lowering prices?")
    cheapest = d.loc[d.groupby(["day", "CountryCode"])[value_col].idxmin()]
    series = cheapest.pivot(index="day", columns="CountryCode", values=value_col)
    st.caption(f"Cheapest {value_label} per country, by day{note}")
    st.line_chart(series)

    st.caption("Who set each day's cheapest price (the shop behind each point above)")
    who = cheapest.pivot(index="day", columns="CountryCode", values="ShopName")
    st.dataframe(who, use_container_width=True)


def view_quality(fdf, snap):
    st.subheader("Data quality & coverage")

    st.caption("Snapshot collapse (guards the daily double-count)")
    c = st.columns(3)
    c[0].metric("Raw observations", f"{len(fdf):,}")
    c[1].metric("After latest-snapshot", f"{len(snap):,}")
    c[2].metric("Rows per combo", f"{len(fdf) / max(len(snap), 1):.1f}×")

    st.caption("Products with data, per country")
    st.bar_chart(snap.groupby("CountryCode")["SymsonProductId"].nunique().sort_values(ascending=False))

    st.caption("RRP coverage — share of snapshot rows that have an RRP (rest have no index)")
    rrp_cov = snap.assign(has=snap["RRP"].notna()).groupby("CountryCode")["has"].mean().sort_values()
    st.dataframe(rrp_cov.round(3).rename("RRP present").to_frame(), use_container_width=True)

    st.caption("Currencies present (EUR figures use the FX table in build_merge)")
    st.dataframe(snap["Currency"].value_counts().rename("rows").to_frame(), use_container_width=True)


# ----------------------------- app shell -------------------------------------
def main():
    st.sidebar.title("Competitor pricing")
    up = st.sidebar.file_uploader("Merged CSV", type="csv")
    if up is None:
        st.info("Upload **custom CSV** to begin.")
        return

    df = load_data(up)
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        st.error("CSV is missing required columns: " + ", ".join(missing))
        return

    s = st.sidebar
    s.markdown("### Filters")
    def ms(col):
        opts = sorted(df[col].dropna().unique())
        return s.multiselect(col, opts, default=opts if len(opts) <= 12 else [])
    brand, main, sub, ctry = ms("Brand"), ms("MainGroup"), ms("SubGroup"), ms("CountryCode")

    fdf = df.copy()
    for col, sel in [("Brand", brand), ("MainGroup", main), ("SubGroup", sub), ("CountryCode", ctry)]:
        if sel:
            fdf = fdf[fdf[col].isin(sel)]

    s.markdown("### Display")
    have_eur = {"Price_EUR", "TotalCost_EUR", "RRP_EUR"}.issubset(df.columns)
    if have_eur:
        eur = s.radio("Currency", ["EUR (converted)", "Local currency"], index=0).startswith("EUR")
    else:
        eur = False
        s.caption("No EUR columns in this CSV — showing local currency. "
                  "Re-run build_merge to enable EUR.")
    cm = cols_for_mode(df, eur)

    s.markdown("### Basis")
    basis = s.radio("Ranking basis", ["Landed (incl. shipping)", "Sticker price"], index=0)
    value_key = "total" if basis.startswith("Landed") else "price"
    value_label = "landed" if basis.startswith("Landed") else "sticker"

    s.markdown("### Flags")
    use_uc = s.checkbox("Flag index below…", value=False)
    undercut = s.slider("…fraction of RRP", 0.4, 1.0, 0.75, 0.05) if use_uc else None
    dp = s.number_input("Decimals", 0, 4, 2)

    if fdf.empty:
        st.warning("No rows match the current filters.")
        return

    snap = latest_snapshot(fdf)

    s.markdown("### Views")
    mode = s.radio("Mode", ["Basic", "Advanced"], index=0, horizontal=True,
                   help="Basic shows the three core views for day-to-day use. "
                        "Advanced adds benchmarking, RRP violators, history and data quality.")
    basic_views = ["Europe overview", "Article", "Competitor"]
    extra_views = ["Benchmarking", "RRP violators", "Price history", "Data quality"]
    views = basic_views if mode == "Basic" else basic_views + extra_views

    # a drill request from the overview forces the radio to Article before it renders
    if st.session_state.pop("_go_article", False):
        st.session_state["view_radio"] = "Article"
    if st.session_state.get("view_radio") not in views:
        st.session_state["view_radio"] = views[0]

    view = s.radio("View", views, key="view_radio")

    if view == "Europe overview":
        view_overview(snap, cm, value_key, value_label, dp)
    elif view == "Article":
        view_article(snap, cm, value_key, value_label, dp, undercut)
    elif view == "Competitor":
        view_competitor(snap, cm, value_key, value_label, dp)
    elif view == "Benchmarking":
        view_benchmark(snap, dp, undercut)
    elif view == "RRP violators":
        view_violators(snap, cm, dp)
    elif view == "Price history":
        view_history(fdf, cm, value_key, value_label)
    else:
        view_quality(fdf, snap)


if __name__ == "__main__":
    main()
