// Wanas Gallery storefront — implementation of "Wanas Store.dc.html", wired
// to the real backend (storefront/api.py) over the same database the
// dashboard and WhatsApp bot use. No client-side catalog data: everything
// here comes from /api/* at runtime.
"use strict";

const h = React.createElement;
const EGP = (n) => "EGP " + n.toLocaleString("en-US");

const eyebrow = (color) => ({
  font: "var(--type-eyebrow)",
  letterSpacing: "var(--ls-eyebrow)",
  textTransform: "uppercase",
  color: color || "var(--text-muted)",
});

// Representative swatch colours for the real garment colours — the catalog
// carries colour names, not hex values.
const SWATCH = {
  Black: '#141614', Grey: '#A9AFA9', Olive: '#5C6B4A', Brown: '#7A5A45',
  Beige: '#DCCDB6', Navy: '#26364F', White: '#FFFFFF', Burgundy: '#6E2A33',
  'Camel Brown': '#B98450', 'Light Brown': '#C9A27A', Pink: '#E9A8B0',
  'Vintage Green': '#7C8A6A'
};

// Curated by the shop, not derived from the catalog's `collection` column
// (which tags a few products the shop doesn't want shown this way, e.g.
// "Zipup", and misses "WANAS Sweatpant"). "Collections" is a shelf, never a
// category filter, so this list is explicit.
const COLLECTIONS = {
  cairokee: { title: 'Cairokee Merch', ids: ['cairokee-hoodie', 'cairokee-tee-2', 'cairokee-tee'] },
  winter: { title: 'Winter Collection', ids: ['wanas-hoodie', 'wanas-zip-hoodie', 'wanas-crewneck', 'wanas-polo', 'wanas-sweatpant', 'wanas-quarter-zip'] }
};

const CATEGORY_ORDER = ['T-Shirts', 'Hoodies & Sweatshirts', 'Polo Shirts', 'Joggers & Sweatpants', 'Jackets', 'Tops'];

// A small downward nudge on the crop so the head clears the top of the tile.
// Pants and the Cairokee collection are framed differently and stay centred.
function cropPosition(productId, category) {
  if (category === 'Joggers & Sweatpants') return 'center';
  if (COLLECTIONS.cairokee.ids.includes(productId)) return 'center';
  return 'center 30%';
}

async function api(path, opts) {
  const res = await fetch(path, {
    credentials: 'same-origin',
    headers: opts && opts.body ? { 'Content-Type': 'application/json' } : undefined,
    ...opts
  });
  return res.json();
}

function imagesFor(p, color) {
  const byColor = p.color_images && p.color_images[color];
  return (byColor && byColor.length) ? byColor : p.images;
}

function orderErrorMessage(result) {
  if (result.error === 'items_out_of_stock') {
    const names = result.items.map(i => i.product_name + ' (' + i.color + ', ' + i.size + ')').join(', ');
    return 'Sold out while you were shopping: ' + names + '. Nothing was charged and your bag is untouched.';
  }
  if (result.error === 'no_rate_set') return 'We can’t ship to that governorate yet — pick another.';
  if (result.error === 'cart_empty') return 'Your bag is empty.';
  if (result.error === 'client_blocked') return 'We’re unable to process this order — please contact us.';
  if (result.error === 'missing_fields') return 'Please fill in all required fields.';
  return 'Something went wrong placing your order. Please try again.';
}

// ---------------------------------------------------------------------
// App
// ---------------------------------------------------------------------

class App extends React.Component {
  constructor(props) {
    super(props);
    this.state = {
      loading: true,
      screen: 'home', category: 'All', collectionFilter: null, showCollectionsMenu: false,
      products: [], governorates: [], cart: { lines: [], item_count: 0, subtotal: 0 },
      productId: null, product: null,
      color: null, size: null, length: null, activeImage: 0,
      showChart: false, sizeChart: null,
      form: { name: '', phone: '', email: '', gov: '', address: '' },
      placing: false, orderError: null, orderResult: null
    };
  }

  async componentDidMount() {
    const [productsRes, govsRes, cart] = await Promise.all([
      api('/api/products'), api('/api/governorates'), api('/api/cart')
    ]);
    this.setState({ loading: false, products: productsRes.products, governorates: govsRes, cart });
  }

  selectedVariant() {
    const { product, color, size, length } = this.state;
    if (!product) return null;
    return product.variants.find(v => v.color === color && v.size === size && (!product.lengths.length || v.length === length));
  }

  open = async (id) => {
    window.scrollTo(0, 0);
    this.setState({ screen: 'product', productId: id, product: null, showChart: false, sizeChart: null, activeImage: 0 });
    const product = await api('/api/products/' + id);
    const first = product.variants.find(v => v.status !== 'sold_out') || product.variants[0];
    this.setState({ product, color: first.color, size: first.size, length: first.length || (product.lengths[0] || null) });
  };

  selectColor = (c) => this.setState({ color: c, activeImage: 0 });

  goHome = () => {
    this.setState({ screen: 'home', category: 'All', collectionFilter: null, showCollectionsMenu: false, orderError: null });
    window.scrollTo(0, 0);
  };

  goList = () => {
    this.setState({ screen: 'list', category: 'All', collectionFilter: null, showCollectionsMenu: false, orderError: null });
    window.scrollTo(0, 0);
  };

  goCategory = (cat) => {
    this.setState({ screen: 'list', category: cat || 'All', collectionFilter: null, showCollectionsMenu: false, orderError: null });
    window.scrollTo(0, 0);
  };

  toggleCollectionsMenu = () => this.setState(st => ({ showCollectionsMenu: !st.showCollectionsMenu }));

  openCollection = (key) => {
    this.setState({ screen: 'list', collectionFilter: key, category: 'All', showCollectionsMenu: false });
    window.scrollTo(0, 0);
  };

  toggleChart = async () => {
    const opening = !this.state.showChart;
    this.setState({ showChart: opening });
    if (opening && !this.state.sizeChart && this.state.productId) {
      const chart = await api('/api/products/' + this.state.productId + '/size-chart');
      this.setState({ sizeChart: chart });
    }
  };

  chipStyle(active) {
    return {
      font: 'var(--type-label)', fontSize: 'var(--fs-xs)', letterSpacing: 'var(--ls-label)', padding: '9px 18px',
      borderRadius: 'var(--r-pill)', cursor: 'pointer', whiteSpace: 'nowrap', transition: 'background var(--dur-base) var(--ease-standard)',
      border: '1px solid ' + (active ? 'transparent' : 'var(--line-hairline)'),
      background: active ? 'var(--action-primary)' : 'transparent',
      color: active ? 'var(--text-inverse)' : 'var(--text-body)'
    };
  }

  optionStyle(state) {
    const base = {
      font: 'var(--type-label)', fontSize: 'var(--fs-xs)', letterSpacing: 'var(--ls-label)',
      padding: '10px 18px', borderRadius: 'var(--r-pill)', cursor: 'pointer', background: 'transparent',
      display: 'inline-flex', alignItems: 'center', gap: 8,
      transition: 'border-color var(--dur-base) var(--ease-standard)', border: '1px solid var(--line-hairline)', color: 'var(--text-body)'
    };
    if (state === 'selected') return { ...base, border: '1px solid var(--forest-700)', color: 'var(--text-strong)', background: 'var(--forest-50)' };
    if (state === 'disabled') return { ...base, opacity: .4, cursor: 'not-allowed', textDecoration: 'line-through' };
    return base;
  }

  addToBag = async () => {
    const sel = this.selectedVariant();
    if (!sel || sel.status === 'sold_out') return;
    const cart = await api('/api/cart/items', { method: 'POST', body: JSON.stringify({ variant_id: sel.variant_id, quantity: 1 }) });
    this.setState({ cart, screen: 'bag' });
    window.scrollTo(0, 0);
  };

  setQty = async (lineId, q) => {
    const cart = await api('/api/cart/items/' + lineId, { method: 'PATCH', body: JSON.stringify({ quantity: q }) });
    this.setState({ cart });
  };

  placeOrder = async () => {
    this.setState({ placing: true, orderError: null });
    const st = this.state;
    const result = await api('/api/orders', {
      method: 'POST',
      body: JSON.stringify({
        customer_name: st.form.name, governorate: st.form.gov, address: st.form.address,
        contact_phone: st.form.phone, email: st.form.email || null
      })
    });
    if (result.error) {
      const cart = result.error === 'items_out_of_stock' ? await api('/api/cart') : st.cart;
      this.setState({ placing: false, orderError: orderErrorMessage(result), cart });
      window.scrollTo(0, 0);
      return;
    }
    this.setState({ placing: false, screen: 'done', orderResult: result, cart: { lines: [], item_count: 0, subtotal: 0 } });
    window.scrollTo(0, 0);
  };

  resolveLink = async (confirmed) => {
    await api('/api/clients/link', { method: 'POST', body: JSON.stringify({ confirmed }) });
    this.setState(st => ({ orderResult: { ...st.orderResult, pending_link: null } }));
  };

  computeVals() {
    const st = this.state;
    const gov = st.governorates.find(g => g.key === st.form.gov) || null;
    const govOk = !!(gov && gov.available && gov.fee != null);
    const complete = !!(st.form.name && st.form.phone && st.form.address && govOk);
    const subtotal = st.cart.subtotal;

    let listing = st.products;
    if (st.collectionFilter) {
      const ids = COLLECTIONS[st.collectionFilter].ids;
      listing = listing.filter(p => ids.includes(p.product_id));
    } else if (st.category !== 'All') {
      listing = listing.filter(p => p.category === st.category);
    }

    const cardFor = (x) => ({
      id: x.product_id, name: x.name, category: x.category,
      price: x.price_from === x.price_to ? EGP(x.price_from) : EGP(x.price_from) + ' – ' + EGP(x.price_to),
      compareAt: x.on_sale ? EGP(x.original_price_to) : null,
      badge: !x.any_in_stock ? 'Sold Out' : (x.on_sale ? 'Sale' : null),
      badgeTone: !x.any_in_stock ? 'neutral' : 'sale',
      tint: 'cream', image: imagesFor(x, x.colors[0])[0], imageLabel: x.name,
      imagePosition: cropPosition(x.product_id, x.category),
      swatches: x.colors.map(c => SWATCH[c] || '#ccc'),
      onClick: () => this.open(x.product_id)
    });

    const homeCategories = CATEGORY_ORDER.map(c => {
      const inCat = st.products.filter(x => x.category === c);
      const rep = inCat[0];
      return {
        label: c, count: inCat.length + (inCat.length === 1 ? ' piece' : ' pieces'),
        tint: 'cream', onClick: () => this.goCategory(c),
        image: rep ? imagesFor(rep, rep.colors[0])[0] : null,
        imagePosition: rep ? cropPosition(rep.product_id, c) : 'center'
      };
    });

    const featuredCards = st.products.filter(x => x.any_in_stock).slice(0, 4).map(cardFor);

    const saleProducts = st.products.filter(x => x.on_sale && x.any_in_stock && x.original_price_to);
    const saleProduct = saleProducts.length
      ? saleProducts.reduce((best, x) => {
          const pct = 1 - x.price_from / x.original_price_to;
          const bestPct = 1 - best.price_from / best.original_price_to;
          return pct > bestPct ? x : best;
        })
      : null;
    const saleDiscountPct = saleProduct ? Math.round((1 - saleProduct.price_from / saleProduct.original_price_to) * 100) : null;
    const promo = saleProduct ? {
      eyebrow: 'On Sale', title: 'Up to ' + saleDiscountPct + '% Off',
      body: 'Including the ' + saleProduct.name + ', now ' + EGP(saleProduct.price_from) + '.',
      cta: 'Shop the Sale', discount: saleDiscountPct + '%',
      image: imagesFor(saleProduct, saleProduct.colors[0])[0],
      imagePosition: 'center 15%',
      imageLabel: saleProduct.name,
      onCta: () => this.goCategory(saleProduct.category)
    } : null;

    const p = st.product;
    const sel = this.selectedVariant();
    const forColor = p ? p.variants.filter(v => v.color === st.color && (!p.lengths.length || v.length === st.length)) : [];

    const colorOptions = p ? p.colors.map(c => {
      const any = p.variants.some(v => v.color === c && v.status !== 'sold_out');
      const state = st.color === c ? 'selected' : (any ? 'default' : 'disabled');
      return { label: c, swatch: SWATCH[c] || '#ccc', style: this.optionStyle(state), onClick: () => this.selectColor(c) };
    }) : [];
    const lengthOptions = p ? p.lengths.map(len => {
      const any = p.variants.some(v => v.color === st.color && v.length === len && v.status !== 'sold_out');
      const state = st.length === len ? 'selected' : (any ? 'default' : 'disabled');
      return { label: len, style: this.optionStyle(state), onClick: () => this.setState({ length: len }) };
    }) : [];
    const sizeOptions = p ? p.sizes.map(s => {
      const v = forColor.find(x => x.size === s);
      const state = st.size === s ? 'selected' : ((!v || v.status === 'sold_out') ? 'disabled' : 'default');
      return { label: s, style: this.optionStyle(state), onClick: () => { if (v && v.status !== 'sold_out') this.setState({ size: s }); } };
    }) : [];

    const availability = !p ? '' : !sel ? 'This size is not made in ' + st.color
      : sel.status === 'sold_out' ? 'Sold out in this combination — try another colour'
      : sel.status === 'low_stock' ? 'Low stock — only a few left'
      : '';

    const chart = st.sizeChart;
    let chartCols = [], chartRows = [];
    if (chart && chart.has_chart) {
      const measurements = chart.measurements.filter(m => !m.applies_to_length || m.applies_to_length === st.length);
      chartCols = ['Size', ...measurements.map(m => m.label_en + (m.marker ? ' (' + m.marker + ')' : ''))];
      chartRows = (p ? p.sizes : []).map(s => [s, ...measurements.map(m => {
        const row = chart.sizes[s];
        return (row && row[m.key] != null) ? row[m.key] + ' ' + chart.unit : '—';
      })]);
    }

    const gallery = p ? imagesFor(p, st.color) : [];
    const activeImage = gallery.length ? Math.min(st.activeImage, gallery.length - 1) : 0;

    return {
      logo: 'assets/wns-logo.png', onLogoClick: this.goHome,
      navLinks: ['Shop', 'Collections'],
      navActive: st.screen === 'list' && !st.collectionFilter ? 'Shop' : null,
      onNav: (label) => { if (label === 'Collections') this.toggleCollectionsMenu(); else this.goList(); },
      showCollectionsMenu: st.showCollectionsMenu,
      bagCount: st.cart.item_count,
      goBag: () => { this.setState({ screen: 'bag' }); window.scrollTo(0, 0); },
      goHome: this.goHome, goList: this.goList, goCategory: this.goCategory,
      goCheckout: () => { this.setState({ screen: 'checkout' }); window.scrollTo(0, 0); },
      isHome: st.screen === 'home',
      isList: st.screen === 'list', isProduct: st.screen === 'product', isBag: st.screen === 'bag',
      isCheckout: st.screen === 'checkout', isDone: st.screen === 'done',
      listTitle: st.collectionFilter ? COLLECTIONS[st.collectionFilter].title : 'Shop All',
      showCategoryChips: !st.collectionFilter,
      sortOptions: [{ value: 'new', label: 'Newest' }, { value: 'low', label: 'Price: Low to High' }, { value: 'high', label: 'Price: High to Low' }],
      categoryChips: CATEGORY_ORDER.length ? ['All', ...CATEGORY_ORDER].map(c => ({ label: c, style: this.chipStyle(st.category === c), onClick: () => this.setState({ category: c }) })) : [],
      listing: listing.map(cardFor),

      homeCategories, featuredCards, promo,

      crumbs: p ? ['Home', 'Shop', p.name] : ['Home'],
      onCrumbNav: (it) => { if (it === 'Home') this.goHome(); else if (it === 'Shop') this.goList(); },
      pTint: 'cream', pCategory: p ? p.category : '', pName: p ? p.name : '', pDescription: p ? p.description : '',
      pPrice: p ? (sel ? EGP(sel.price) : EGP(p.price_from)) : '',
      pCompare: sel && sel.on_sale ? EGP(sel.original_price) : '',
      priceNote: sel && sel.on_sale ? 'Was ' + EGP(sel.original_price) + ' · price shown is for the selected colour' : 'Price shown is for the selected colour',
      selColor: st.color, selSize: st.size, selLength: st.length,
      colorOptions, sizeOptions, lengthOptions, availabilityNote: availability,
      gallery, activeImage, imagePosition: p ? cropPosition(p.product_id, p.category) : 'center',
      setActiveImage: (i) => this.setState({ activeImage: i }),
      showChart: st.showChart, toggleChart: this.toggleChart, hasSizeChart: p && p.has_size_chart,
      chartTitle: chart && chart.has_chart ? chart.title + ' · ' + chart.unit : '', chartImage: chart && chart.has_chart ? chart.image : null,
      chartCols, chartRows,
      addDisabled: !sel || sel.status === 'sold_out',
      addLabel: !p ? '' : (!sel || sel.status === 'sold_out') ? 'Sold Out' : 'Add to Bag · ' + EGP(sel.price),
      addToBag: this.addToBag,

      cartLines: st.cart.lines.map(l => ({
        lineId: l.line_id, name: l.product_name, image: l.image, qty: l.quantity,
        meta: [l.size, l.color, l.length].filter(Boolean).join(' · ') + ' · ' + EGP(l.unit_price),
        summaryLabel: l.product_name + ' · ' + l.size + ' ' + l.color + ' × ' + l.quantity,
        total: EGP(l.line_total),
        statusLabel: l.status === 'sold_out' ? 'Sold out — remove to continue' : (l.status === 'low_stock' ? 'Low stock' : ''),
        statusColor: l.status === 'sold_out' ? 'var(--status-error)' : 'var(--ink-500)',
        onQty: q => this.setQty(l.line_id, q)
      })),
      bagEmpty: st.cart.lines.length === 0,
      subtotalLabel: EGP(subtotal),
      shippingLabel: govOk ? EGP(gov.fee) : 'Pick a governorate',
      totalLabel: st.screen === 'done' && st.orderResult ? EGP(st.orderResult.total) : (govOk ? EGP(subtotal + gov.fee) : EGP(subtotal)),

      govOptions: [{ value: '', label: 'Select governorate' }, ...st.governorates.map(g => ({
        value: g.key,
        label: g.available && g.fee != null ? g.key + ' · ' + g.label_ar : g.key + ' · ' + g.label_ar + ' — no delivery yet'
      }))],
      govNote: !st.form.gov ? 'Shipping fee appears once you choose' : (govOk ? 'Shipping to ' + gov.key + ' · ' + EGP(gov.fee) : 'We don’t deliver to ' + st.form.gov + ' yet'),
      govNoteColor: st.form.gov && !govOk ? 'var(--status-error)' : 'var(--text-muted)',
      formName: st.form.name, formPhone: st.form.phone, formEmail: st.form.email,
      formGov: st.form.gov, formAddress: st.form.address,
      onName: e => this.setState({ form: { ...st.form, name: e.target.value } }),
      onPhone: e => this.setState({ form: { ...st.form, phone: e.target.value } }),
      onEmail: e => this.setState({ form: { ...st.form, email: e.target.value } }),
      onGov: e => this.setState({ form: { ...st.form, gov: e.target.value } }),
      onAddress: e => this.setState({ form: { ...st.form, address: e.target.value } }),
      orderError: st.orderError,
      placeDisabled: !complete || st.placing,
      placeOrder: this.placeOrder,
      orderResult: st.orderResult,
      resolveLink: this.resolveLink,

      footerItems: [
        { icon: 'truck', title: 'Delivery 2–5 days', note: 'All 27 governorates' },
        { icon: 'banknote', title: 'Cash on delivery', note: 'Pay when it arrives' }
      ],
      footerColumns: [
        { title: 'Shop', links: CATEGORY_ORDER.map(c => ({ label: c, onClick: () => this.goCategory(c) })) },
        { title: 'Collections', links: Object.entries(COLLECTIONS).map(([key, c]) => ({ label: c.title, onClick: () => this.openCollection(key) })) }
      ]
    };
  }

  // -- screen renderers ---------------------------------------------

  renderList(v) {
    const backLink = !v.showCategoryChips ? h('button', {
      onClick: v.goList,
      style: { background: 'none', border: 0, padding: 0, cursor: 'pointer', font: 'var(--type-label)', fontSize: 'var(--fs-xs)', color: 'var(--forest-700)', marginBottom: 8, display: 'block' }
    }, '← All products') : null;
    return h('div', { style: { maxWidth: 1180, margin: '0 auto', padding: 'var(--sp-7) var(--gutter-page) 80px' } },
      h('div', { style: { display: 'flex', alignItems: 'flex-end', justifyContent: 'space-between', gap: 24, marginBottom: 'var(--sp-6)', flexWrap: 'wrap' } },
        h('div', null,
          backLink,
          h('h1', { style: { font: 'var(--type-h2)', color: 'var(--text-strong)', margin: 0 } }, v.listTitle)
        ),
        h(DS.Select, { options: v.sortOptions, style: { width: 180 } })
      ),
      v.showCategoryChips ? h('div', { style: { display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 'var(--sp-6)' } },
        v.categoryChips.map(chip => h('button', { key: chip.label, onClick: chip.onClick, style: chip.style }, chip.label))
      ) : null,
      h('div', { className: 'wanas-grid-4 wanas-product-grid', style: { display: 'grid', gridTemplateColumns: 'repeat(4,minmax(0,1fr))', gap: 16 } },
        v.listing.map(p => h('div', { key: p.id, style: { position: 'relative' } },
          h(DS.ProductCard, { ...p })
        ))
      )
    );
  }

  renderProduct(v) {
    if (!v.pName) {
      return h('div', { style: { padding: '120px 0', textAlign: 'center', color: 'var(--text-muted)' } }, 'Loading…');
    }
    return h('div', { style: { maxWidth: 1180, margin: '0 auto', padding: 'var(--sp-5) var(--gutter-page) 80px' } },
      h(DS.Breadcrumb, { items: v.crumbs, onNav: v.onCrumbNav }),
      h('div', { className: 'wanas-bag-grid', style: { display: 'grid', gridTemplateColumns: 'minmax(0,1.1fr) minmax(0,1fr)', gap: 48, marginTop: 'var(--sp-5)', alignItems: 'start' } },
        h('div', null,
          h(DS.ImagePlaceholder, { tint: v.pTint, ratio: '1/1', label: v.pName, src: v.gallery[v.activeImage], objectPosition: v.imagePosition }),
          v.gallery.length > 1 ? h('div', { style: { display: 'flex', gap: 8, marginTop: 10, flexWrap: 'wrap' } },
            v.gallery.map((src, i) => h('button', {
              key: i, onClick: () => v.setActiveImage(i),
              style: {
                width: 64, height: 64, padding: 0, cursor: 'pointer', background: 'none', borderRadius: 'var(--r-sm)', overflow: 'hidden',
                border: '2px solid ' + (i === v.activeImage ? 'var(--forest-700)' : 'transparent')
              }
            }, h(DS.ImagePlaceholder, { tint: v.pTint, ratio: '1/1', label: '', src, objectPosition: v.imagePosition, style: { width: '100%', height: '100%' } })))
          ) : null
        ),
        h('div', null,
          h('div', { style: eyebrow() }, v.pCategory),
          h('h1', { style: { font: 'var(--type-h2)', color: 'var(--text-strong)', margin: '8px 0 14px' } }, v.pName),
          h('div', { style: { display: 'flex', alignItems: 'baseline', gap: 12, marginBottom: 6 } },
            h('span', { style: { font: 'var(--type-h3)', color: 'var(--text-price)' } }, v.pPrice),
            v.pCompare ? h('span', { style: { font: 'var(--type-label)', color: 'var(--text-muted)', textDecoration: 'line-through' } }, v.pCompare) : null
          ),
          h('div', { style: { ...eyebrow(), marginBottom: 24 } }, v.priceNote),
          h('p', { style: { font: 'var(--type-body)', color: 'var(--text-body)', lineHeight: 1.6, margin: '0 0 28px', maxWidth: '44ch' } }, v.pDescription),
          h('div', { style: { font: 'var(--type-label)', color: 'var(--text-strong)', marginBottom: 10 } }, 'Colour · ' + v.selColor),
          h('div', { style: { display: 'flex', flexWrap: 'wrap', gap: 10, marginBottom: v.lengthOptions.length ? 18 : 26 } },
            v.colorOptions.map(c => h('button', { key: c.label, onClick: c.onClick, style: c.style },
              h('span', { style: { width: 12, height: 12, borderRadius: '50%', background: c.swatch, boxShadow: 'var(--shadow-inset-hairline)', flex: '0 0 auto' } }),
              c.label
            ))
          ),
          v.lengthOptions.length ? h('div', { style: { font: 'var(--type-label)', color: 'var(--text-strong)', marginBottom: 10 } }, 'Sleeve · ' + v.selLength) : null,
          v.lengthOptions.length ? h('div', { style: { display: 'flex', flexWrap: 'wrap', gap: 10, marginBottom: 26 } },
            v.lengthOptions.map(l => h('button', { key: l.label, onClick: l.onClick, style: l.style }, l.label))
          ) : null,
          h('div', { style: { display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', marginBottom: 10 } },
            h('span', { style: { font: 'var(--type-label)', color: 'var(--text-strong)' } }, 'Size · ' + v.selSize),
            v.hasSizeChart ? h('button', { onClick: v.toggleChart, style: { background: 'none', border: 0, padding: 0, cursor: 'pointer', font: 'var(--type-label)', fontSize: 'var(--fs-xs)', color: 'var(--forest-700)', borderBottom: '1px solid var(--forest-200)' } }, 'Size chart') : null
          ),
          h('div', { style: { display: 'flex', flexWrap: 'wrap', gap: 10, marginBottom: 12 } },
            v.sizeOptions.map(s => h('button', { key: s.label, onClick: s.onClick, style: s.style }, s.label))
          ),
          v.availabilityNote ? h('div', { style: { ...eyebrow(), marginBottom: 26 } }, v.availabilityNote) : h('div', { style: { marginBottom: 26 } }),
          v.showChart ? h('div', { style: { background: 'var(--surface-card)', borderRadius: 'var(--r-card)', boxShadow: 'var(--shadow-2)', padding: 20, marginBottom: 26 } },
            !v.chartTitle ? h('div', { style: eyebrow() }, 'Loading…') : h(React.Fragment, null,
              h('div', { style: { font: 'var(--type-label)', color: 'var(--text-strong)', marginBottom: 12 } }, v.chartTitle),
              h('div', { style: { display: 'grid', gridTemplateColumns: 'repeat(' + v.chartCols.length + ',minmax(0,1fr))', gap: 0, font: 'var(--type-body)', fontSize: 'var(--fs-sm)' } },
                v.chartCols.map((c, i) => h('div', { key: 'h' + i, style: { padding: '8px 6px', borderBottom: '1px solid var(--line-hairline)', font: 'var(--type-eyebrow)', letterSpacing: 'var(--ls-eyebrow)', textTransform: 'uppercase', color: 'var(--text-muted)' } }, c)),
                v.chartRows.map((row, ri) => row.map((cell, ci) => h('div', { key: ri + '-' + ci, style: { padding: '9px 6px', borderBottom: '1px solid var(--line-hairline)', color: ci === 0 ? 'var(--text-strong)' : 'var(--text-body)' } }, cell)))
              ),
              v.chartImage ? h('img', { src: v.chartImage, alt: v.chartTitle, style: { width: '100%', borderRadius: 'var(--r-sm)', marginTop: 16, display: 'block' } }) : null,
              h('div', { style: { ...eyebrow(), marginTop: 14, lineHeight: 1.6 } }, 'Garment measurements, laid flat — not body measurements. مقاسات المنتج مفرود.')
            )
          ) : null,
          h('div', { style: { display: 'flex', gap: 12, alignItems: 'center' } },
            h(DS.Button, { variant: 'primary', size: 'lg', disabled: v.addDisabled, onClick: v.addToBag, style: { width: '100%' } }, v.addLabel)
          ),
          h('div', { style: { display: 'flex', gap: 20, marginTop: 22, flexWrap: 'wrap' } },
            h(DS.TrustItem, { icon: 'truck', title: 'Delivery 2–5 days', note: 'Fee shown at checkout' }),
            h(DS.TrustItem, { icon: 'banknote', title: 'Cash on delivery', note: 'Pay when it arrives' })
          )
        )
      )
    );
  }

  renderBag(v) {
    return h('div', { style: { maxWidth: 1180, margin: '0 auto', padding: 'var(--sp-7) var(--gutter-page) 80px' } },
      h('h1', { style: { font: 'var(--type-h2)', color: 'var(--text-strong)', margin: '0 0 28px' } }, 'Your Bag'),
      h('div', { className: 'wanas-bag-grid', style: { display: 'grid', gridTemplateColumns: 'minmax(0,1.6fr) minmax(0,1fr)', gap: 32, alignItems: 'start' } },
        h('div', { style: { display: 'flex', flexDirection: 'column', gap: 14 } },
          v.cartLines.map(l => h('div', {
            key: l.lineId, style: { background: 'var(--surface-card)', borderRadius: 'var(--r-card)', boxShadow: 'var(--shadow-2)', padding: 16, display: 'grid', gridTemplateColumns: '96px minmax(0,1fr) auto', gap: 18, alignItems: 'center' }
          },
            h(DS.ImagePlaceholder, { tint: 'cream', ratio: '1/1', label: '', src: l.image, style: { width: 96, height: 96 } }),
            h('div', null,
              h('div', { style: { font: 'var(--type-body)', color: 'var(--text-strong)' } }, l.name),
              h('div', { style: { font: 'var(--type-body)', fontSize: 'var(--fs-sm)', color: 'var(--text-muted)', margin: '4px 0 10px' } }, l.meta),
              l.statusLabel ? h('div', { style: eyebrow(l.statusColor) }, l.statusLabel) : null
            ),
            h('div', { style: { display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 12 } },
              h('span', { style: { font: 'var(--type-price)', color: 'var(--text-price)' } }, l.total),
              h(DS.QuantityStepper, { value: l.qty, min: 0, onChange: l.onQty })
            )
          )),
          v.bagEmpty ? h('div', { style: { background: 'var(--surface-card)', borderRadius: 'var(--r-card)', boxShadow: 'var(--shadow-2)', padding: 48, textAlign: 'center' } },
            h('div', { style: { font: 'var(--type-body)', color: 'var(--text-muted)', marginBottom: 18 } }, 'Your bag is empty.'),
            h(DS.Button, { variant: 'secondary', onClick: v.goList }, 'Continue Shopping')
          ) : null
        ),
        h('div', { style: { background: 'var(--surface-card)', borderRadius: 'var(--r-card)', boxShadow: 'var(--shadow-2)', padding: 24, position: 'sticky', top: 100 } },
          h('div', { style: { font: 'var(--type-label)', color: 'var(--text-strong)', marginBottom: 16 } }, 'Summary'),
          h('div', { style: { display: 'flex', justifyContent: 'space-between', font: 'var(--type-body)', marginBottom: 10 } },
            h('span', null, 'Subtotal'), h('span', { style: { font: 'var(--type-price)' } }, v.subtotalLabel)),
          h('div', { style: { display: 'flex', justifyContent: 'space-between', font: 'var(--type-body)', fontSize: 'var(--fs-sm)', color: 'var(--text-muted)', paddingBottom: 16, borderBottom: '1px solid var(--line-hairline)' } },
            h('span', null, 'Shipping'), h('span', null, 'Calculated at checkout')),
          h('div', { style: { marginTop: 20 } },
            h(DS.Button, { variant: 'primary', size: 'lg', fullWidth: true, disabled: v.bagEmpty, onClick: v.goCheckout }, 'Checkout')),
          h('div', { style: { ...eyebrow(), marginTop: 14, textAlign: 'center' } }, 'Cash on delivery only')
        )
      )
    );
  }

  renderCheckout(v) {
    return h('div', { style: { maxWidth: 960, margin: '0 auto', padding: 'var(--sp-7) var(--gutter-page) 80px' } },
      h('h1', { style: { font: 'var(--type-h2)', color: 'var(--text-strong)', margin: '0 0 28px' } }, 'Checkout'),
      h('div', { className: 'wanas-checkout-grid', style: { display: 'grid', gridTemplateColumns: 'minmax(0,1.3fr) minmax(0,1fr)', gap: 32, alignItems: 'start' } },
        h('div', { style: { background: 'var(--surface-card)', borderRadius: 'var(--r-card)', boxShadow: 'var(--shadow-2)', padding: 24, display: 'flex', flexDirection: 'column', gap: 14 } },
          v.orderError ? h('div', { style: { background: 'var(--blush-100)', borderRadius: 'var(--r-tile)', padding: '16px 18px' } },
            h('div', { style: { font: 'var(--type-body)', fontSize: 'var(--fs-sm)', color: 'var(--text-body)', lineHeight: 1.6 } }, v.orderError)
          ) : null,
          h('div', { style: { font: 'var(--type-label)', color: 'var(--text-strong)' } }, 'Delivery details'),
          h(DS.Input, { placeholder: 'Full name', value: v.formName, onChange: v.onName }),
          h(DS.Input, { placeholder: 'Phone number', value: v.formPhone, onChange: v.onPhone }),
          h(DS.Input, { placeholder: 'Email (optional)', value: v.formEmail, onChange: v.onEmail }),
          h(DS.Select, { options: v.govOptions, value: v.formGov, onChange: v.onGov, style: { width: '100%' } }),
          h('div', { style: eyebrow(v.govNoteColor) }, v.govNote),
          h(DS.Input, { placeholder: 'Street address, building, apartment', value: v.formAddress, onChange: v.onAddress }),
          h('div', { style: { font: 'var(--type-label)', color: 'var(--text-strong)', marginTop: 10 } }, 'Payment'),
          h('div', { style: { border: '1px solid var(--forest-500)', borderRadius: 'var(--r-input)', padding: '14px 18px', font: 'var(--type-body)', color: 'var(--text-strong)' } }, 'Cash on delivery')
        ),
        h('div', { style: { background: 'var(--surface-card)', borderRadius: 'var(--r-card)', boxShadow: 'var(--shadow-2)', padding: 24 } },
          h('div', { style: { font: 'var(--type-label)', color: 'var(--text-strong)', marginBottom: 16 } }, 'Order summary'),
          v.cartLines.map(l => h('div', { key: l.lineId, style: { display: 'flex', justifyContent: 'space-between', gap: 16, font: 'var(--type-body)', fontSize: 'var(--fs-sm)', marginBottom: 10 } },
            h('span', { style: { color: 'var(--text-body)' } }, l.summaryLabel), h('span', null, l.total)
          )),
          h('div', { style: { height: 1, background: 'var(--line-hairline)', margin: '16px 0' } }),
          h('div', { style: { display: 'flex', justifyContent: 'space-between', font: 'var(--type-body)', fontSize: 'var(--fs-sm)', marginBottom: 8 } }, h('span', null, 'Subtotal'), h('span', null, v.subtotalLabel)),
          h('div', { style: { display: 'flex', justifyContent: 'space-between', font: 'var(--type-body)', fontSize: 'var(--fs-sm)', marginBottom: 8 } }, h('span', null, 'Shipping'), h('span', null, v.shippingLabel)),
          h('div', { style: { display: 'flex', justifyContent: 'space-between', font: 'var(--type-price)', color: 'var(--text-strong)', margin: '16px 0 20px' } }, h('span', null, 'Total'), h('span', null, v.totalLabel)),
          h(DS.Button, { variant: 'primary', size: 'lg', fullWidth: true, disabled: v.placeDisabled, onClick: v.placeOrder }, 'Place Order')
        )
      )
    );
  }

  renderDone(v) {
    const r = v.orderResult;
    if (!r) return null;
    return h('div', { style: { maxWidth: 640, margin: '0 auto', padding: '80px var(--gutter-page)', textAlign: 'center' } },
      h('div', { style: { ...eyebrow(), marginBottom: 14 } }, 'Order ' + r.order_id + ' · Confirmed'),
      h('h1', { style: { font: 'var(--type-h2)', color: 'var(--text-strong)', margin: '0 0 16px' } }, "Thank you. We're packing it now."),
      h('p', { style: { font: 'var(--type-body)', color: 'var(--text-body)', lineHeight: 1.6, margin: '0 0 32px' } },
        "You'll pay " + EGP(r.total) + ' in cash when it arrives. We’ll message you with each step.'),
      r.pending_link ? h('div', { style: { background: 'var(--forest-50)', borderRadius: 'var(--r-tile)', padding: '16px 18px', textAlign: 'left', marginBottom: 24 } },
        h('div', { style: { font: 'var(--type-label)', color: 'var(--forest-900)', marginBottom: 6 } }, 'Is this you? ' + r.pending_link.masked_name),
        h('div', { style: { font: 'var(--type-body)', fontSize: 'var(--fs-sm)', color: 'var(--text-body)', lineHeight: 1.6, marginBottom: 12 } }, "This " + r.pending_link.matched_on + " matches an existing customer. Confirm and we'll remember your address next time."),
        h('div', { style: { display: 'flex', gap: 10 } },
          h(DS.Button, { variant: 'primary', size: 'sm', onClick: () => v.resolveLink(true) }, "Yes, that's me"),
          h(DS.Button, { variant: 'ghost', size: 'sm', onClick: () => v.resolveLink(false) }, 'Not me')
        )
      ) : null,
      h(DS.Button, { variant: 'secondary', onClick: v.goList }, 'Continue Shopping')
    );
  }

  renderHome(v) {
    return h('div', { style: { maxWidth: 1180, margin: '0 auto', padding: 'var(--sp-7) var(--gutter-page) var(--sp-10)', display: 'grid', gridTemplateColumns: 'minmax(0,1fr)', gap: 'var(--sp-9)' } },
      h('div', { className: 'wanas-hero', style: { position: 'relative', display: 'grid', gridTemplateColumns: 'minmax(0,1fr) minmax(0,1fr)', gap: 'var(--sp-7)', alignItems: 'center', minHeight: 400, minWidth: 0 } },
        h('div', null,
          h('div', { style: eyebrow() }, 'WANAS'),
          h('h1', { style: { font: 'var(--fw-regular) var(--fs-hero)/var(--lh-tight) var(--font-display)', letterSpacing: 'var(--ls-display)', color: 'var(--text-strong)', margin: 'var(--sp-5) 0' } }, 'Wanas Is a Feel.'),
          h('p', { style: { font: 'var(--type-body)', fontSize: 'var(--fs-body-lg)', color: 'var(--text-body)', maxWidth: 340, margin: '0 0 var(--sp-6)' } }, 'Oversized, boxy cuts. Cash on delivery, anywhere in Egypt.'),
          h(DS.Button, { size: 'lg', onClick: v.goList }, 'Shop Now')
        ),
        h('div', { style: { position: 'relative', height: 400, minWidth: 0 } },
          h('div', { style: { position: 'absolute', right: 40, top: 0, width: 280, height: 280, borderRadius: 'var(--r-circle)', background: 'var(--blush-300)' } }),
          h('video', {
            src: 'assets/hero.mp4', autoPlay: true, muted: true, loop: true, playsInline: true,
            style: {
              position: 'absolute', inset: '20px 40px 0 20px', width: 'calc(100% - 60px)', height: 'calc(100% - 20px)',
              objectFit: 'cover', objectPosition: 'center 20%', borderRadius: 'var(--r-panel)', boxShadow: 'var(--shadow-float)'
            }
          })
        )
      ),

      h('div', null,
        h('div', { style: { marginBottom: 'var(--sp-5)' } }, h(DS.SectionHeader, { title: 'Shop by Category', linkLabel: 'Browse all', onLink: v.goList })),
        h('div', { className: 'wanas-category-row', style: { display: 'grid', gridTemplateColumns: 'repeat(' + v.homeCategories.length + ',minmax(0,1fr))', gap: 'var(--sp-5)', justifyItems: 'center', marginTop: 'var(--sp-6)' } },
          v.homeCategories.map(cat => h(DS.CategoryCircle, { key: cat.label, ...cat }))
        )
      ),

      v.promo ? h(DS.PromoBanner, { ...v.promo }) : null,

      v.featuredCards.length ? h('div', null,
        h('div', { style: { marginBottom: 'var(--sp-5)' } }, h(DS.SectionHeader, { title: 'New This Season', linkLabel: 'View all', onLink: v.goList })),
        h('div', { className: 'wanas-grid-4 wanas-product-grid', style: { display: 'grid', gridTemplateColumns: 'repeat(4,minmax(0,1fr))', gap: 'var(--gap-grid)', marginTop: 'var(--sp-6)' } },
          v.featuredCards.map(p => h(DS.ProductCard, { key: p.id, ...p }))
        )
      ) : null
    );
  }

  render() {
    if (this.state.loading) {
      return h('div', { style: { minHeight: '100vh', display: 'grid', placeItems: 'center', font: 'var(--type-body)', color: 'var(--text-muted)' } }, 'Loading…');
    }
    const v = this.computeVals();
    return h('div', { style: { minHeight: '100vh', background: 'var(--surface-page)', color: 'var(--text-body)', font: 'var(--type-body)' } },
      h('div', { style: { position: 'sticky', top: 0, zIndex: 20 } },
        h('div', { style: { borderBottom: '1px solid var(--line-hairline)' } },
          h(DS.Header, { links: v.navLinks, active: v.navActive, onNav: v.onNav, logo: v.logo, onLogoClick: v.onLogoClick, bagCount: v.bagCount, onBag: v.goBag })
        ),
        v.showCollectionsMenu ? h('div', {
          style: {
            background: 'var(--surface-card)', borderBottom: '1px solid var(--line-hairline)', boxShadow: 'var(--shadow-2)',
            padding: '16px var(--gutter-page)', display: 'flex', gap: 28, flexWrap: 'wrap'
          }
        },
          Object.entries(COLLECTIONS).map(([key, c]) => h('button', {
            key, onClick: () => this.openCollection(key),
            style: { background: 'none', border: 0, cursor: 'pointer', font: 'var(--type-label)', color: 'var(--text-strong)' }
          }, c.title))
        ) : null
      ),
      v.isHome ? this.renderHome(v) : null,
      v.isList ? this.renderList(v) : null,
      v.isProduct ? this.renderProduct(v) : null,
      v.isBag ? this.renderBag(v) : null,
      v.isCheckout ? this.renderCheckout(v) : null,
      v.isDone ? this.renderDone(v) : null,
      h('div', { style: { maxWidth: 1180, margin: '0 auto' } },
        h(DS.Footer, { items: v.footerItems, columns: v.footerColumns, logo: v.logo })
      )
    );
  }
}

const root = document.getElementById('root');
if (ReactDOM.createRoot) ReactDOM.createRoot(root).render(h(App));
else ReactDOM.render(h(App), root);
