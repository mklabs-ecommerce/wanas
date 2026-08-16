// Wanas design system components, vendored from the Claude Design bundle
// (wanas-design-system-d57f552d-5e51-4e46-83cb-4a2a64ad4792) for standalone
// use without the dc-runtime. Plain React.createElement, no JSX/build step.
"use strict";
(function () {
  const DS = (window.DS = window.DS || {});

  const tints = {
    cream: 'var(--cream-200)',
    blush: 'var(--blush-100)',
    sage: 'var(--sage-200)',
    forest: 'var(--forest-50)',
    gold: 'var(--gold-100)'
  };

  function ImagePlaceholder({ tint = 'cream', radius = 'var(--r-tile)', label, ratio, src, objectFit = 'cover', objectPosition = 'center', children, style }) {
    const [broken, setBroken] = React.useState(false);
    const showPhoto = src && !broken;
    return React.createElement('div', {
      style: {
        position: 'relative',
        background: tints[tint] || tint,
        borderRadius: radius,
        aspectRatio: ratio,
        display: 'grid',
        placeItems: 'center',
        overflow: 'hidden',
        width: '100%',
        height: ratio ? undefined : '100%',
        ...style
      }
    },
      showPhoto ? React.createElement('img', {
        src, alt: label || '', loading: 'lazy', onError: () => setBroken(true),
        style: { position: 'absolute', inset: 0, width: '100%', height: '100%', objectFit, objectPosition }
      }) : (label ? React.createElement('span', {
        style: {
          font: 'var(--type-eyebrow)',
          letterSpacing: 'var(--ls-eyebrow)',
          textTransform: 'uppercase',
          color: 'var(--text-muted)',
          opacity: .7,
          textAlign: 'center',
          padding: '0 var(--sp-3)'
        }
      }, label) : null),
      children
    );
  }

  const badgeTones = {
    sale: { background: 'var(--status-sale)', color: '#fff' },
    new: { background: 'var(--status-new)', color: 'var(--forest-900)' },
    low: { background: 'var(--status-lowstock)', color: 'var(--forest-900)' },
    neutral: { background: 'var(--cream-200)', color: 'var(--text-body)' },
    inverse: { background: 'var(--forest-700)', color: 'var(--text-inverse)' }
  };
  function Badge({ children, tone = 'neutral', style }) {
    return React.createElement('span', {
      style: {
        ...badgeTones[tone],
        font: 'var(--type-eyebrow)',
        letterSpacing: 'var(--ls-label)',
        textTransform: 'uppercase',
        padding: '5px 10px',
        borderRadius: 'var(--r-pill)',
        display: 'inline-block',
        ...style
      }
    }, children);
  }

  function Icon({ name, size = 18, stroke = 1.5, color = 'currentColor', style }) {
    const ref = React.useRef(null);
    React.useEffect(() => {
      const draw = () => {
        const L = window.lucide;
        if (!L || !ref.current) return;
        const icons = L.icons || {};
        const key = name.replace(/(^|-)([a-z])/g, (m, a, b) => b.toUpperCase());
        const node = icons[key];
        if (!node) return;
        ref.current.innerHTML = L.createElement ? L.createElement(node).outerHTML : '';
      };
      draw();
      const t = setTimeout(draw, 300);
      return () => clearTimeout(t);
    }, [name]);
    return React.createElement('span', {
      ref,
      'data-icon': name,
      style: { display: 'inline-flex', width: size, height: size, color, strokeWidth: stroke, ...style }
    });
  }

  function IconButton({ children, label, size = 40, variant = 'ghost', onClick, badge, style }) {
    const [h, setH] = React.useState(false);
    const bg = { ghost: 'transparent', solid: 'var(--surface-card)', accent: 'var(--surface-accent-soft)' }[variant];
    return React.createElement('button', {
      'aria-label': label,
      onClick,
      onMouseEnter: () => setH(true),
      onMouseLeave: () => setH(false),
      style: {
        position: 'relative',
        width: size,
        height: size,
        borderRadius: 'var(--r-circle)',
        border: variant === 'solid' ? '1px solid var(--line-hairline)' : '1px solid transparent',
        background: h ? 'var(--cream-200)' : bg,
        color: 'var(--text-strong)',
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        cursor: 'pointer',
        transition: 'background var(--dur-base) var(--ease-standard)',
        boxShadow: variant === 'solid' ? 'var(--shadow-1)' : 'none',
        ...style
      }
    }, children, badge ? React.createElement('span', {
      style: {
        position: 'absolute', top: 2, right: 2, minWidth: 16, height: 16, padding: '0 4px',
        borderRadius: 'var(--r-pill)', background: 'var(--action-accent)', color: 'var(--forest-900)',
        font: 'var(--type-eyebrow)', display: 'grid', placeItems: 'center'
      }
    }, badge) : null);
  }

  function Logotype({ size = 22, inverse = false, style }) {
    return React.createElement('span', {
      style: {
        font: 'var(--fw-regular) ' + size + 'px var(--font-display)',
        letterSpacing: '0.16em',
        textTransform: 'uppercase',
        color: inverse ? 'var(--text-inverse)' : 'var(--text-strong)',
        ...style
      }
    }, 'Wanas');
  }

  const btnBase = {
    font: 'var(--type-label)', letterSpacing: 'var(--ls-label)', borderRadius: 'var(--r-button)',
    border: '1px solid transparent', cursor: 'pointer', display: 'inline-flex', alignItems: 'center',
    justifyContent: 'center', gap: 'var(--sp-3)',
    transition: 'background var(--dur-base) var(--ease-standard),transform var(--dur-fast) var(--ease-standard),color var(--dur-base) var(--ease-standard)',
    textDecoration: 'none', whiteSpace: 'nowrap'
  };
  const btnSizes = {
    sm: { padding: '8px 16px', fontSize: 'var(--fs-xs)' },
    md: { padding: '12px 24px', fontSize: 'var(--fs-sm)' },
    lg: { padding: '16px 32px', fontSize: 'var(--fs-body)' }
  };
  const btnVariants = {
    primary: { background: 'var(--action-primary)', color: 'var(--text-inverse)' },
    accent: { background: 'var(--action-accent)', color: 'var(--text-on-accent)' },
    secondary: { background: 'transparent', color: 'var(--text-strong)', borderColor: 'var(--line-strong)' },
    ghost: { background: 'transparent', color: 'var(--text-strong)' },
    onDark: { background: 'var(--cream-50)', color: 'var(--forest-900)' }
  };
  const btnHovers = {
    primary: 'var(--action-primary-hover)', accent: 'var(--action-accent-hover)',
    secondary: 'var(--cream-200)', ghost: 'var(--cream-200)', onDark: '#FFFFFF'
  };
  function Button({ children, variant = 'primary', size = 'md', iconRight, iconLeft, disabled = false, fullWidth = false, as = 'button', href, onClick, style }) {
    const [h, setH] = React.useState(false);
    const [p, setP] = React.useState(false);
    const v = btnVariants[variant] || btnVariants.primary;
    const Tag = as === 'a' ? 'a' : 'button';
    return React.createElement(Tag, {
      href, onClick, disabled: Tag === 'button' ? disabled : undefined,
      onMouseEnter: () => setH(true),
      onMouseLeave: () => { setH(false); setP(false); },
      onMouseDown: () => setP(true),
      onMouseUp: () => setP(false),
      style: {
        ...btnBase, ...btnSizes[size], ...v,
        width: fullWidth ? '100%' : undefined,
        background: h && !disabled ? btnHovers[variant] : v.background,
        transform: p ? 'var(--press-scale)' : 'none',
        opacity: disabled ? .4 : 1,
        pointerEvents: disabled ? 'none' : undefined,
        ...style
      }
    }, iconLeft, React.createElement('span', null, children), iconRight);
  }

  function TrustItem({ icon, title, note, tint = 'gold', style }) {
    const tt = { gold: 'var(--gold-100)', sage: 'var(--sage-200)', blush: 'var(--blush-100)', forest: 'var(--forest-50)' };
    return React.createElement('div', {
      style: { display: 'flex', alignItems: 'center', gap: 'var(--sp-3)', ...style }
    },
      React.createElement('span', {
        style: {
          width: 38, height: 38, flex: '0 0 auto', borderRadius: 'var(--r-circle)',
          background: tt[tint], display: 'grid', placeItems: 'center', color: 'var(--forest-700)'
        }
      }, React.createElement(Icon, { name: icon, size: 18 })),
      React.createElement('div', null,
        React.createElement('div', { style: { font: 'var(--type-label)', color: 'var(--text-strong)' } }, title),
        React.createElement('div', { style: { font: 'var(--type-body)', fontSize: 'var(--fs-xs)', color: 'var(--text-muted)' } }, note)
      )
    );
  }

  function ProductCard({ name, price, compareAt, badge, badgeTone = 'sale', tint = 'cream', image, imageLabel, imagePosition = 'center', swatches = [], wishlisted = false, onWishlist, onClick, style }) {
    const [h, setH] = React.useState(false);
    return React.createElement('div', {
      onClick, onMouseEnter: () => setH(true), onMouseLeave: () => setH(false),
      style: {
        background: 'var(--surface-card)', borderRadius: 'var(--r-card)', padding: 'var(--sp-3)',
        cursor: 'pointer', boxShadow: h ? 'var(--shadow-3)' : 'var(--shadow-1)',
        transform: h ? 'var(--hover-lift)' : 'none',
        transition: 'box-shadow var(--dur-base) var(--ease-standard),transform var(--dur-base) var(--ease-out-soft)',
        ...style
      }
    },
      React.createElement('div', { style: { position: 'relative', marginBottom: 'var(--sp-3)' } },
        React.createElement(ImagePlaceholder, { tint, ratio: '1/1', label: imageLabel, src: image, objectPosition: imagePosition }),
        badge ? React.createElement(Badge, { tone: badgeTone, style: { position: 'absolute', top: 10, left: 10 } }, badge) : null,
        React.createElement('button', {
          onClick: e => { e.stopPropagation(); onWishlist && onWishlist(); },
          'aria-label': 'Save',
          style: {
            position: 'absolute', top: 8, right: 8, width: 30, height: 30, borderRadius: 'var(--r-circle)',
            border: 0, background: 'var(--veil-light)', backdropFilter: 'var(--blur-veil)', cursor: 'pointer',
            display: 'grid', placeItems: 'center', color: wishlisted ? 'var(--status-sale)' : 'var(--text-body)'
          }
        }, React.createElement(Icon, { name: 'heart', size: 14 }))
      ),
      React.createElement('div', { style: { padding: '0 var(--sp-2) var(--sp-2)' } },
        React.createElement('div', { style: { font: 'var(--type-body)', color: 'var(--text-strong)', marginBottom: 2 } }, name),
        React.createElement('div', { style: { display: 'flex', alignItems: 'center', gap: 'var(--sp-2)' } },
          React.createElement('span', { style: { font: 'var(--type-price)', color: 'var(--text-price)' } }, price),
          compareAt ? React.createElement('span', { style: { font: 'var(--type-label)', color: 'var(--text-muted)', textDecoration: 'line-through' } }, compareAt) : null
        ),
        swatches.length ? React.createElement('div', { style: { display: 'flex', gap: 5, marginTop: 'var(--sp-3)' } },
          swatches.map((c, i) => React.createElement('span', {
            key: i, style: { width: 8, height: 8, borderRadius: 'var(--r-circle)', background: c, boxShadow: 'var(--shadow-inset-hairline)' }
          }))
        ) : null
      )
    );
  }

  function Checkbox({ label, checked = false, onChange, style }) {
    return React.createElement('label', {
      style: { display: 'inline-flex', alignItems: 'center', gap: 'var(--sp-3)', cursor: 'pointer', font: 'var(--type-body)', color: 'var(--text-body)', ...style }
    },
      React.createElement('span', {
        onClick: () => onChange && onChange(!checked),
        style: {
          width: 18, height: 18, borderRadius: 'var(--r-xs)', flex: '0 0 auto',
          border: '1px solid ' + (checked ? 'var(--forest-700)' : 'var(--line-strong)'),
          background: checked ? 'var(--forest-700)' : 'transparent',
          display: 'grid', placeItems: 'center', transition: 'all var(--dur-fast) var(--ease-standard)'
        }
      }, checked ? React.createElement('svg', { width: 11, height: 11, viewBox: '0 0 24 24', fill: 'none', stroke: '#fff', strokeWidth: 3 },
        React.createElement('path', { d: 'M20 6L9 17l-5-5' })) : null),
      label
    );
  }

  function Input({ placeholder, value, onChange, type = 'text', size = 'md', iconLeft, onDark = false, style }) {
    const [f, setF] = React.useState(false);
    const pads = { sm: '9px 14px', md: '13px 18px', lg: '16px 22px' };
    return React.createElement('div', {
      style: {
        display: 'flex', alignItems: 'center', gap: 'var(--sp-3)', padding: pads[size],
        background: onDark ? 'rgba(255,255,255,.9)' : 'var(--surface-card)', borderRadius: 'var(--r-input)',
        border: '1px solid ' + (f ? 'var(--focus-ring)' : 'var(--line-hairline)'),
        transition: 'border-color var(--dur-base) var(--ease-standard)', ...style
      }
    }, iconLeft, React.createElement('input', {
      type, placeholder, value, onChange, onFocus: () => setF(true), onBlur: () => setF(false),
      style: { border: 0, outline: 'none', background: 'transparent', font: 'var(--type-body)', color: 'var(--text-strong)', width: '100%' }
    }));
  }

  function QuantityStepper({ value = 1, min = 1, onChange, style }) {
    const btn = {
      width: 32, height: 32, borderRadius: 'var(--r-circle)', border: '1px solid var(--line-hairline)',
      background: 'var(--surface-card)', cursor: 'pointer', font: 'var(--type-label)', color: 'var(--text-strong)'
    };
    return React.createElement('div', { style: { display: 'inline-flex', alignItems: 'center', gap: 'var(--sp-3)', ...style } },
      React.createElement('button', { style: btn, onClick: () => onChange && onChange(Math.max(min, value - 1)) }, '–'),
      React.createElement('span', { style: { font: 'var(--type-price)', minWidth: 20, textAlign: 'center' } }, value),
      React.createElement('button', { style: btn, onClick: () => onChange && onChange(value + 1) }, '+')
    );
  }

  function Select({ options = [], value, onChange, style }) {
    return React.createElement('select', {
      value, onChange,
      style: {
        appearance: 'none', font: 'var(--type-body)', color: 'var(--text-strong)', padding: '12px 40px 12px 18px',
        borderRadius: 'var(--r-input)', border: '1px solid var(--line-hairline)', background: 'var(--surface-card)',
        cursor: 'pointer',
        backgroundImage: 'linear-gradient(45deg,transparent 50%,var(--text-muted) 50%),linear-gradient(135deg,var(--text-muted) 50%,transparent 50%)',
        backgroundPosition: 'calc(100% - 22px) 50%,calc(100% - 17px) 50%',
        backgroundSize: '5px 5px,5px 5px', backgroundRepeat: 'no-repeat', ...style
      }
    }, options.map(o => {
      const v = (o != null && typeof o === 'object') ? (o.value !== undefined ? o.value : '') : o;
      const label = (o != null && typeof o === 'object') ? (o.label !== undefined ? o.label : v) : o;
      return React.createElement('option', { key: v, value: v }, label);
    }));
  }

  function Breadcrumb({ items = [], onNav, style }) {
    return React.createElement('nav', {
      style: { display: 'flex', alignItems: 'center', gap: 'var(--sp-2)', font: 'var(--type-body)', fontSize: 'var(--fs-sm)', color: 'var(--text-muted)', ...style }
    }, items.map((it, i) => React.createElement(React.Fragment, { key: it },
      i ? React.createElement('span', { style: { opacity: .6 } }, '/') : null,
      React.createElement('button', {
        onClick: () => onNav && onNav(it),
        style: { background: 'none', border: 0, padding: 0, cursor: 'pointer', font: 'inherit', color: i === items.length - 1 ? 'var(--text-strong)' : 'inherit' }
      }, it)
    )));
  }

  function Header({ links = ['Shop', 'Collections', 'New Arrivals', 'Deals', 'About'], active, onNav, onLogoClick, logo, bagCount = 0, onBag, onSearch, onAccount, style }) {
    return React.createElement('header', {
      style: {
        display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: 'var(--sp-5) var(--gutter-page)',
        background: 'var(--veil-light)', backdropFilter: 'var(--blur-veil)', flexWrap: 'wrap', gap: 'var(--sp-4)', ...style
      }
    },
      React.createElement('button', {
        onClick: onLogoClick, 'aria-label': 'Wanas — home',
        style: { display: 'flex', alignItems: 'center', gap: 'var(--sp-3)', background: 'none', border: 0, padding: 0, cursor: onLogoClick ? 'pointer' : 'default' }
      },
        logo
          ? React.createElement('img', { src: logo, alt: 'Wanas', style: { height: 44, width: 'auto', display: 'block' } })
          : [
              React.createElement('span', { key: 'i', style: { color: 'var(--forest-700)', display: 'inline-flex' } }, React.createElement(Icon, { name: 'flower', size: 22 })),
              React.createElement(Logotype, { key: 'l' })
            ]
      ),
      React.createElement('nav', { style: { display: 'flex', gap: 'var(--sp-7)', flexWrap: 'wrap' } },
        links.map(l => React.createElement('button', {
          key: l, onClick: () => onNav && onNav(l),
          style: {
            background: 'none', border: 0, cursor: 'pointer', font: 'var(--type-body)', fontSize: 'var(--fs-sm)',
            color: active === l ? 'var(--text-strong)' : 'var(--text-body)',
            borderBottom: '1px solid ' + (active === l ? 'var(--forest-700)' : 'transparent'), paddingBottom: 2
          }
        }, l))
      ),
      React.createElement('div', { style: { display: 'flex', gap: 'var(--sp-1)' } },
        React.createElement(IconButton, { label: 'Search', onClick: onSearch }, React.createElement(Icon, { name: 'search', size: 18 })),
        React.createElement(IconButton, { label: 'Account', onClick: onAccount }, React.createElement(Icon, { name: 'user', size: 18 })),
        React.createElement(IconButton, { label: 'Bag', onClick: onBag, badge: bagCount || undefined }, React.createElement(Icon, { name: 'shopping-bag', size: 18 }))
      )
    );
  }

  function Card({ children, tone = 'card', pad = true, float = false, style }) {
    const tones = {
      card: { background: 'var(--surface-card)' },
      sunken: { background: 'var(--surface-sunken)' },
      accent: { background: 'var(--surface-accent)' },
      inverse: { background: 'var(--surface-inverse)', color: 'var(--text-inverse)' }
    };
    return React.createElement('div', {
      style: {
        borderRadius: 'var(--r-card)', padding: pad ? 'var(--pad-card)' : 0,
        boxShadow: float ? 'var(--shadow-float)' : 'var(--shadow-card)',
        ...tones[tone], ...style
      }
    }, children);
  }

  function SectionHeader({ title, eyebrow, linkLabel, onLink, inverse = false, style }) {
    return React.createElement('div', {
      style: { display: 'flex', alignItems: 'flex-end', justifyContent: 'space-between', gap: 'var(--sp-6)', ...style }
    },
      React.createElement('div', null,
        eyebrow ? React.createElement('div', {
          style: { font: 'var(--type-eyebrow)', letterSpacing: 'var(--ls-eyebrow)', textTransform: 'uppercase', color: inverse ? 'var(--blush-300)' : 'var(--text-muted)', marginBottom: 'var(--sp-3)' }
        }, eyebrow) : null,
        React.createElement('h2', {
          style: { font: 'var(--type-h2)', letterSpacing: 'var(--ls-display)', margin: 0, color: inverse ? 'var(--text-inverse)' : 'var(--text-strong)' }
        }, title)
      ),
      linkLabel ? React.createElement('button', {
        onClick: onLink,
        style: { background: 'none', border: 0, cursor: 'pointer', font: 'var(--type-label)', color: inverse ? 'var(--text-inverse)' : 'var(--text-body)', display: 'inline-flex', alignItems: 'center', gap: 'var(--sp-2)' }
      }, linkLabel, React.createElement(Icon, { name: 'arrow-right', size: 16 })) : null
    );
  }

  function Tag({ children, selected = false, onClick, style }) {
    return React.createElement('button', {
      onClick,
      style: {
        font: 'var(--type-label)', padding: '8px 16px', borderRadius: 'var(--r-pill)', cursor: 'pointer',
        border: '1px solid ' + (selected ? 'var(--forest-700)' : 'var(--line-hairline)'),
        background: selected ? 'var(--forest-700)' : 'transparent',
        color: selected ? 'var(--text-inverse)' : 'var(--text-body)',
        transition: 'all var(--dur-base) var(--ease-standard)', ...style
      }
    }, children);
  }

  function PriceRow({ label, value, strong = false, muted = false, style }) {
    return React.createElement('div', {
      style: { display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', font: strong ? 'var(--type-h3)' : 'var(--type-body)', color: muted ? 'var(--text-muted)' : 'var(--text-strong)', ...style }
    }, React.createElement('span', null, label), React.createElement('span', { style: { fontVariantNumeric: 'tabular-nums' } }, value));
  }

  function CategoryCircle({ label, count, tint = 'cream', size = 120, image, imagePosition, onClick, style }) {
    const [h, setH] = React.useState(false);
    return React.createElement('button', {
      onClick, onMouseEnter: () => setH(true), onMouseLeave: () => setH(false),
      style: { background: 'none', border: 0, cursor: 'pointer', display: 'grid', justifyItems: 'center', gap: 'var(--sp-3)', ...style }
    },
      React.createElement(ImagePlaceholder, {
        tint, radius: 'var(--r-circle)', label: image ? '' : label, src: image, objectPosition: imagePosition,
        style: { width: size, height: size, transform: h ? 'scale(1.03)' : 'none', transition: 'transform var(--dur-base) var(--ease-out-soft)' }
      }),
      React.createElement('div', { style: { textAlign: 'center' } },
        React.createElement('div', { style: { font: 'var(--type-body)', color: 'var(--text-strong)' } }, label),
        count ? React.createElement('div', { style: { font: 'var(--type-label)', fontWeight: 'var(--fw-light)', color: 'var(--text-muted)', marginTop: 2 } }, count) : null
      )
    );
  }

  function PromoBanner({ eyebrow, title, body, cta, discount, imageLabel, image, imagePosition, onCta, style }) {
    return React.createElement('div', {
      style: { display: 'grid', gridTemplateColumns: '1fr 1fr', background: 'var(--surface-inverse)', borderRadius: 'var(--r-card)', overflow: 'hidden', minHeight: 200, ...style }
    },
      React.createElement('div', { style: { padding: 'var(--sp-8)' } },
        eyebrow ? React.createElement('div', { style: { font: 'var(--type-eyebrow)', letterSpacing: 'var(--ls-eyebrow)', textTransform: 'uppercase', color: 'var(--gold-400)', marginBottom: 'var(--sp-4)' } }, eyebrow) : null,
        React.createElement('h3', { style: { font: 'var(--type-h2)', color: 'var(--text-inverse)', margin: '0 0 var(--sp-3)' } }, title),
        body ? React.createElement('p', { style: { font: 'var(--type-body)', color: 'var(--forest-200)', margin: '0 0 var(--sp-6)', maxWidth: 280 } }, body) : null,
        cta ? React.createElement(Button, { variant: 'accent', onClick: onCta }, cta) : null
      ),
      React.createElement('div', { style: { position: 'relative' } },
        React.createElement(ImagePlaceholder, { tint: 'blush', radius: '0', label: image ? '' : imageLabel, src: image, objectPosition: imagePosition }),
        discount ? React.createElement('div', {
          style: {
            position: 'absolute', left: -40, top: '50%', transform: 'translateY(-50%)', width: 110, height: 110,
            borderRadius: 'var(--r-circle)', background: 'var(--action-accent)', color: 'var(--forest-900)',
            display: 'grid', placeItems: 'center', textAlign: 'center', lineHeight: 1.05
          }
        }, React.createElement('div', null,
          React.createElement('div', { style: { font: 'var(--type-eyebrow)', letterSpacing: 'var(--ls-label)', textTransform: 'uppercase' } }, 'up to'),
          React.createElement('div', { style: { font: 'var(--fw-medium) 30px var(--font-sans)' } }, discount),
          React.createElement('div', { style: { font: 'var(--type-eyebrow)', letterSpacing: 'var(--ls-label)', textTransform: 'uppercase' } }, 'off')
        )) : null
      )
    );
  }

  function Footer({ items = [], columns = [], logo, style }) {
    return React.createElement('footer', {
      style: { padding: 'var(--sp-8) var(--gutter-page)', borderTop: '1px solid var(--line-hairline)', ...style }
    },
      items.length ? React.createElement('div', {
        style: { display: 'grid', gridTemplateColumns: 'repeat(' + items.length + ',1fr)', gap: 'var(--sp-7)', paddingBottom: 'var(--sp-7)' }
      }, items.map((it, i) => React.createElement(TrustItem, { key: i, ...it }))) : null,
      columns.length ? React.createElement('div', {
        style: { display: 'grid', gridTemplateColumns: '1.4fr repeat(' + columns.length + ',1fr)', gap: 'var(--sp-7)', paddingTop: 'var(--sp-7)', borderTop: '1px solid var(--line-hairline)' }
      },
        React.createElement('div', null,
          logo ? React.createElement('img', { src: logo, alt: 'Wanas', style: { height: 22, width: 'auto', display: 'block' } }) : React.createElement(Logotype, null),
          React.createElement('p', { style: { font: 'var(--type-body)', fontSize: 'var(--fs-sm)', color: 'var(--text-muted)', maxWidth: 220, marginTop: 'var(--sp-3)' } }, 'Curated styles, quality you love, delivered to your door.')
        ),
        columns.map(c => React.createElement('div', { key: c.title },
          React.createElement('div', { style: { font: 'var(--type-eyebrow)', letterSpacing: 'var(--ls-eyebrow)', textTransform: 'uppercase', color: 'var(--text-muted)', marginBottom: 'var(--sp-4)' } }, c.title),
          React.createElement('div', { style: { display: 'grid', gap: 'var(--sp-3)' } },
            (c.links || []).map(l => (typeof l === 'object'
              ? React.createElement('button', { key: l.label, onClick: l.onClick, style: { background: 'none', border: 0, padding: 0, textAlign: 'left', cursor: 'pointer', font: 'var(--type-body)', fontSize: 'var(--fs-sm)', color: 'var(--text-body)' } }, l.label)
              : React.createElement('span', { key: l, style: { font: 'var(--type-body)', fontSize: 'var(--fs-sm)', color: 'var(--text-body)' } }, l)
            ))
          )
        ))
      ) : null
    );
  }

  Object.assign(DS, {
    ImagePlaceholder, Badge, Icon, IconButton, Logotype, Button, TrustItem,
    ProductCard, Checkbox, Input, QuantityStepper, Select, Breadcrumb, Header,
    Card, SectionHeader, Tag, PriceRow, CategoryCircle, PromoBanner, Footer
  });
})();
