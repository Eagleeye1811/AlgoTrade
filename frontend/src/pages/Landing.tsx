import { useEffect } from 'react'
import { Link } from 'react-router-dom'
import { Activity, ShieldCheck, Smartphone, ArrowRight, CheckCircle2, ServerCrash, CandlestickChart, LayoutDashboard } from 'lucide-react'

export default function Landing() {
  useEffect(() => {
    document.documentElement.style.scrollBehavior = 'smooth'
    return () => { document.documentElement.style.scrollBehavior = 'auto' }
  }, [])

  return (
    <div className="min-h-screen bg-bg text-text font-sans selection:bg-info/30">
      {/* NAVIGATION */}
      <nav className="sticky top-0 z-50 border-b border-white/5 bg-bg/90 backdrop-blur-md">
        <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
          <div className="flex h-16 items-center justify-between">
            <div className="flex items-center gap-2.5">
              <img src="/logo.png" alt="AlgoTrade" className="h-8 w-8 rounded-full object-contain" />
              <span className="text-lg font-bold tracking-tight text-white">AlgoTrade</span>
            </div>
            <div className="hidden md:flex items-center gap-8 text-sm font-medium">
              <a href="#platform" className="hover:text-white transition-colors">Platform</a>
              <a href="#strategies" className="hover:text-white transition-colors">Strategies</a>
              <a href="#risk" className="hover:text-white transition-colors">Risk Management</a>
              <a href="#how-it-works" className="hover:text-white transition-colors">How It Works</a>
            </div>
            <div className="flex items-center gap-4">
              <Link to="/login" className="text-sm font-medium hover:text-white transition-colors">Sign In</Link>
              <Link to="/login" className="rounded-md bg-info px-4 py-2 text-sm font-semibold text-white hover:bg-info transition-colors">
                Open Dashboard
              </Link>
            </div>
          </div>
        </div>
      </nav>

      {/* HERO SECTION */}
      <section className="relative overflow-hidden pt-24 pb-16 md:pt-32 md:pb-24">
        <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
          <div className="grid gap-12 lg:grid-cols-2 lg:gap-8 items-center">
            <div className="max-w-2xl">
              <div className="mb-4 inline-flex items-center rounded-full border border-info/30 bg-info/10 px-3 py-1 text-xs font-semibold text-info uppercase tracking-widest">
                ALGORITHMIC TRADING • INTELLIGENT RISK CONTROL
              </div>
              <h1 className="text-4xl md:text-5xl lg:text-6xl font-bold tracking-tight text-white mb-6 leading-tight">
                Trade with strategy.<br />
                <span className="text-info">Operate with confidence.</span>
              </h1>
              <p className="text-lg text-muted mb-8 max-w-xl leading-relaxed">
                Run multiple trading strategies, monitor live markets, and manage execution risk through one intelligent trading platform.
              </p>
              <div className="flex flex-wrap items-center gap-4 mb-10">
                <Link to="/login" className="flex items-center gap-2 rounded-md bg-info px-6 py-3 text-sm font-semibold text-white hover:bg-info transition-colors">
                  Explore the Platform <ArrowRight size={16} />
                </Link>
                <a href="#how-it-works" className="rounded-md border border-line bg-panel2/50 px-6 py-3 text-sm font-semibold text-white hover:bg-panel2 border-line transition-colors">
                  View How It Works
                </a>
              </div>
              <div className="flex flex-col sm:flex-row gap-4 sm:gap-8 text-sm font-medium text-muted">
                <span className="flex items-center gap-2"><CheckCircle2 size={16} className="text-info" /> Multi-strategy execution</span>
                <span className="flex items-center gap-2"><CheckCircle2 size={16} className="text-info" /> Centralized risk controls</span>
                <span className="flex items-center gap-2"><CheckCircle2 size={16} className="text-info" /> Transparent order lifecycle</span>
              </div>
            </div>

            {/* HERO PREVIEW (Photo-realistic Chart) */}
            <div className="relative mx-auto w-full h-[400px] lg:h-[500px] flex items-center justify-center lg:-mr-12 opacity-90 hover:opacity-100 transition-opacity duration-700">
              {/* Fade masks to blend edges into background */}
              <div className="absolute inset-0 z-10 pointer-events-none" style={{ boxShadow: 'inset 0 0 100px 40px #0a0e14' }}></div>
              
              <svg className="w-full h-full" viewBox="0 0 800 500" preserveAspectRatio="none">
                {/* Background Grid */}
                {[...Array(10)].map((_, i) => (
                  <line key={`h-${i}`} x1="0" y1={i * 50} x2="800" y2={i * 50} stroke="rgba(31,42,55,0.45)" strokeWidth="1" />
                ))}
                {[...Array(16)].map((_, i) => (
                  <line key={`v-${i}`} x1={i * 50} y1="0" x2={i * 50} y2="500" stroke="rgba(31,42,55,0.45)" strokeWidth="1" />
                ))}
                
                {/* Watermark */}
                <text x="400" y="270" fill="rgba(31,42,55,0.45)" fontSize="120" fontWeight="bold" fontFamily="sans-serif" textAnchor="middle" opacity="0.3">RELIANCE · 1m</text>

                {/* Crosshair */}
                <line x1="0" y1="260" x2="800" y2="260" stroke="#475569" strokeDasharray="4 4" strokeWidth="1" />
                <line x1="320" y1="0" x2="320" y2="500" stroke="#475569" strokeDasharray="4 4" strokeWidth="1" />

                {/* Candles and Volume */}
                {[
                  { o: 150, c: 180, h: 130, l: 200, v: 40 },
                  { o: 180, c: 120, h: 100, l: 210, v: 75 },
                  { o: 120, c: 140, h: 110, l: 150, v: 35 },
                  { o: 140, c: 200, h: 120, l: 220, v: 60 },
                  { o: 200, c: 230, h: 190, l: 250, v: 45 },
                  { o: 230, c: 250, h: 210, l: 260, v: 30 },
                  { o: 250, c: 220, h: 210, l: 270, v: 50 },
                  { o: 220, c: 180, h: 170, l: 240, v: 80 },
                  { o: 180, c: 190, h: 160, l: 200, v: 35 },
                  { o: 190, c: 220, h: 170, l: 230, v: 40 },
                  { o: 220, c: 240, h: 210, l: 250, v: 25 },
                  { o: 240, c: 260, h: 230, l: 280, v: 55 },
                  { o: 260, c: 290, h: 250, l: 310, v: 70 },
                  { o: 290, c: 330, h: 270, l: 350, v: 95 }, // Sharp drop
                  { o: 330, c: 310, h: 290, l: 360, v: 60 },
                  { o: 310, c: 340, h: 300, l: 370, v: 50 },
                  { o: 340, c: 360, h: 330, l: 380, v: 40 },
                  { o: 360, c: 250, h: 230, l: 370, v: 110 }, // Big green recovery
                  { o: 250, c: 260, h: 220, l: 280, v: 45 },
                  { o: 260, c: 220, h: 200, l: 270, v: 65 },
                  { o: 220, c: 240, h: 210, l: 250, v: 35 },
                  { o: 240, c: 220, h: 200, l: 260, v: 50 },
                  { o: 220, c: 210, h: 190, l: 230, v: 30 }, // T candle
                  { o: 210, c: 240, h: 200, l: 260, v: 45 },
                  { o: 240, c: 260, h: 230, l: 270, v: 60 },
                  { o: 260, c: 250, h: 240, l: 280, v: 35 },
                  { o: 250, c: 230, h: 220, l: 270, v: 40 },
                  { o: 230, c: 250, h: 220, l: 260, v: 30 },
                  { o: 250, c: 300, h: 240, l: 320, v: 85 }, // Drop
                  { o: 300, c: 290, h: 280, l: 330, v: 45 },
                  { o: 290, c: 320, h: 280, l: 340, v: 60 },
                  { o: 320, c: 310, h: 290, l: 330, v: 35 },
                ].map((c, i) => {
                  const x = i * 24 + 20;
                  const isGreen = c.c <= c.o; // In SVG, lower Y is higher value
                  const color = isGreen ? '#22c55e' : '#ef4444';
                  const bodyY = Math.min(c.o, c.c);
                  const bodyH = Math.max(Math.abs(c.o - c.c), 2);
                  const volH = c.v;
                  const volY = 500 - volH;
                  
                  return (
                    <g key={i}>
                      {/* Volume */}
                      <rect x={x - 4} y={volY} width="8" height={volH} fill={color} opacity="0.3" />
                      {/* Wick */}
                      <line x1={x} y1={c.h} x2={x} y2={c.l} stroke={color} strokeWidth="1.5" />
                      {/* Body */}
                      <rect x={x - 5} y={bodyY} width="10" height={bodyH} fill={color} rx="1" />
                    </g>
                  );
                })}

                {/* EMA Line */}
                <polyline points="20,180 68,150 116,160 164,190 212,230 260,200 308,230 356,260 404,300 452,240 500,230 548,220 596,240 644,260 692,280 740,310 788,300" fill="none" stroke="#3b82f6" strokeWidth="2" strokeOpacity="0.8" />

                {/* Signal Markers */}
                <g transform="translate(560, 210)">
                  <text x="0" y="0" fill="#94A3B8" fontSize="11" fontFamily="monospace">SELL macd no trade</text>
                  <line x1="30" y1="5" x2="30" y2="15" stroke="#94A3B8" strokeWidth="1.5" />
                  <polygon points="27,12 33,12 30,17" fill="#94A3B8" />
                </g>
                <g transform="translate(620, 240)">
                  <text x="0" y="0" fill="#94A3B8" fontSize="11" fontFamily="monospace">SELL rsi_ema no trade</text>
                  <line x1="40" y1="5" x2="40" y2="15" stroke="#94A3B8" strokeWidth="1.5" />
                  <polygon points="37,12 43,12 40,17" fill="#94A3B8" />
                </g>
              </svg>
            </div>
          </div>
        </div>
      </section>

      {/* MARKET SNAPSHOT */}
      <section className="border-y border-line bg-bg py-6 shadow-inner">
        <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
          <div className="flex flex-col md:flex-row items-center gap-6">
            <div className="flex items-center gap-2 pr-6 border-b md:border-b-0 md:border-r border-line pb-4 md:pb-0 shrink-0">
              <span className="relative flex h-2.5 w-2.5">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-info opacity-75"></span>
                <span className="relative inline-flex rounded-full h-2.5 w-2.5 bg-info"></span>
              </span>
              <span className="text-muted uppercase text-xs font-semibold tracking-widest">Demo Market Data</span>
            </div>
            <div className="flex flex-wrap md:flex-nowrap items-center justify-center md:justify-start gap-4 w-full">
              {[
                { sym: 'NIFTY50', px: '22,450.00', chg: '+250.40', pct: '+1.13%', up: true },
                { sym: 'RELIANCE', px: '2,950.45', chg: '+22.10', pct: '+0.75%', up: true },
                { sym: 'HDFCBANK', px: '1,440.10', chg: '-5.30', pct: '-0.37%', up: false },
                { sym: 'TCS', px: '3,890.00', chg: '+14.50', pct: '+0.37%', up: true },
                { sym: 'INFY', px: '1,420.75', chg: '-16.20', pct: '-1.13%', up: false },
              ].map(m => (
                <div key={m.sym} className="flex flex-col justify-center px-4 py-2 rounded-lg bg-panel border border-line hover:border-line transition-colors flex-1 min-w-[140px]">
                  <span className="text-muted text-xs font-medium tracking-wide mb-1">{m.sym}</span>
                  <div className="flex items-baseline justify-between gap-3">
                    <span className="text-white font-mono text-sm">{m.px}</span>
                    <span className={`text-[11px] font-bold ${m.up ? 'text-pos' : 'text-neg'}`}>
                      {m.pct}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      </section>

      {/* CAPABILITIES */}
      <section id="platform" className="py-24 bg-bg">
        <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
          <div className="text-center mb-16">
            <h2 className="text-3xl font-bold text-white mb-4">Everything your trading system needs. Nothing hidden.</h2>
            <p className="text-muted max-w-2xl mx-auto">A unified architecture combining automated strategy execution, aggressive risk controls, and complete operational transparency.</p>
          </div>
          <div className="grid md:grid-cols-2 gap-8">
            {[
              { title: 'Multi-Strategy Trading', desc: 'Operate RSI + EMA, MACD, and Breakout strategies while inspecting their signals, parameters, charts, and execution history in real time.', icon: CandlestickChart },
              { title: 'Intelligent Risk Management', desc: 'The Risk Firewall validates orders against daily-loss limits, position sizes, order frequency, and exposure limits before execution.', icon: ShieldCheck },
              { title: 'Execution & Recovery', desc: 'Tracks order acknowledgements, partial fills, rejections, cancellations, and reconciliation while recovering gracefully from exchange or broker failures.', icon: Activity },
              { title: 'Portfolio Visibility', desc: 'Inspect positions, margin utilization, realized and unrealized P&L, order history, and strategy-level exposure from one dashboard.', icon: LayoutDashboard },
            ].map(f => (
              <div key={f.title} className="group p-8 rounded-2xl border border-line bg-panel hover:border-info/50 hover:bg-panel transition-all">
                <f.icon className="text-info mb-5" size={32} />
                <h3 className="text-xl font-bold text-white mb-3">{f.title}</h3>
                <p className="text-muted leading-relaxed">{f.desc}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* STRATEGIES */}
      <section id="strategies" className="py-24 bg-bg relative overflow-hidden">
        <div className="absolute -top-40 -right-40 w-96 h-96 bg-info/10 blur-[100px] rounded-full pointer-events-none"></div>
        <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8 relative z-10">
          <div className="mb-16">
            <h2 className="text-3xl font-bold text-white mb-4">Three strategies. One controlled environment.</h2>
            <p className="text-muted max-w-2xl">Deploy, configure, and monitor proven technical strategies isolated within their own risk partitions.</p>
          </div>
          <div className="grid lg:grid-cols-3 gap-6">
            {[
              { name: 'RSI + EMA', tags: ['Momentum', 'Trend'], feats: ['RSI oscillator visualization', 'Fast and slow EMA overlays', 'Configurable parameters'], color: 'from-info/20' },
              { name: 'MACD', tags: ['Trend', 'Crossovers'], feats: ['MACD line & signal line', 'Histogram visualization', 'Configurable parameters'], color: 'from-pos/20' },
              { name: 'Breakout', tags: ['Support', 'Resistance'], feats: ['Price breakout detection', 'Volume confirmation', 'Entry and exit rules'], color: 'from-warn/20' },
            ].map((s, i) => (
              <div key={s.name} className="group flex flex-col rounded-2xl border border-line bg-panel overflow-hidden hover:border-info/30 hover:shadow-[0_8px_30px_rgba(59,130,246,0.1)] hover:-translate-y-1 transition-all duration-300">
                <div className={`h-32 bg-gradient-to-b ${s.color} to-panel border-b border-line p-6 flex flex-col justify-end relative overflow-hidden`}>
                  <svg className="absolute inset-0 w-full h-full opacity-10 group-hover:opacity-20 transition-opacity duration-500" viewBox="0 0 100 100" preserveAspectRatio="none">
                    <polyline points={i === 0 ? "0,80 20,60 40,70 60,40 80,50 100,20" : i === 1 ? "0,50 20,40 40,60 60,30 80,70 100,40" : "0,90 20,80 40,80 60,30 80,20 100,10"} fill="none" stroke="currentColor" strokeWidth="2" className="text-info"/>
                  </svg>
                  <div className="flex gap-2 mb-2 relative z-10">
                    {s.tags.map(t => <span key={t} className="text-[10px] uppercase font-bold tracking-wider text-text bg-panel2/80 px-2 py-0.5 rounded border border-line">{t}</span>)}
                  </div>
                  <h3 className="text-xl font-bold text-white relative z-10">{s.name}</h3>
                </div>
                <div className="p-6 flex-1 flex flex-col">
                  <ul className="space-y-3 mb-8 flex-1">
                    {s.feats.map(f => (
                      <li key={f} className="flex items-start gap-2 text-sm text-muted">
                        <CheckCircle2 size={16} className="text-info shrink-0 mt-0.5 opacity-70" /> {f}
                      </li>
                    ))}
                  </ul>
                  <Link to="/login" className="text-sm font-semibold text-info hover:text-info/80 flex items-center gap-1 group-hover:gap-2 transition-all">Configure {s.name} <ArrowRight size={14} /></Link>
                </div>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* RISK MANAGEMENT */}
      <section id="risk" className="py-24 bg-bg border-y border-line">
        <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
          <div className="grid lg:grid-cols-2 gap-16 items-center">
            <div>
              <h2 className="text-3xl font-bold text-white mb-6">Risk controls are part of the architecture.</h2>
              <p className="text-muted mb-8 leading-relaxed">
                AlgoTrade doesn't just execute orders; it validates them against a rigorous set of constraints before they ever reach the broker.
              </p>
              <ul className="space-y-6">
                {[
                  { title: 'Central Risk Firewall', desc: 'Validates every order against daily-loss limits, position sizing, and exposure rules.' },
                  { title: 'Virtual-Margin Partitioning', desc: 'Gives each strategy an independent capital allocation and margin limit.' },
                  { title: 'Strategy Isolation', desc: "Prevents one strategy's internal positions and P&L from contaminating another's state." },
                  { title: 'Emergency Kill Switch', desc: 'Blocks new orders, cancels pending orders, and initiates position closure instantly.' },
                ].map(r => (
                  <li key={r.title}>
                    <h4 className="text-white font-semibold mb-1">{r.title}</h4>
                    <p className="text-muted text-sm leading-relaxed">{r.desc}</p>
                  </li>
                ))}
              </ul>
            </div>
            <div className="rounded-2xl border border-line bg-panel p-8">
              <div className="flex flex-col gap-4">
                <div className="rounded-lg border border-line bg-panel2/50 p-4 text-center">
                  <div className="text-xs font-mono text-muted mb-1">STEP 1</div>
                  <div className="font-bold text-white">Strategy Signal</div>
                </div>
                <div className="flex justify-center text-muted/50"><ArrowRight className="rotate-90" size={20} /></div>
                
                <div className="rounded-lg border border-info/50 bg-info/10 p-4 text-center relative">
                  <div className="text-xs font-mono text-info mb-1">STEP 2</div>
                  <div className="font-bold text-white flex items-center justify-center gap-2"><ShieldCheck size={18} className="text-info"/> Risk Validation</div>
                  
                  {/* Rejected Path */}
                  <div className="absolute right-0 top-1/2 translate-x-full -translate-y-1/2 flex items-center pl-2">
                    <ArrowRight className="text-muted/50 mr-2" size={16} />
                    <div className="rounded border border-neg/30 bg-neg/10 px-2 py-1 text-[10px] font-bold text-neg uppercase">Rejected</div>
                  </div>
                </div>
                
                <div className="flex justify-center text-muted/50"><ArrowRight className="rotate-90" size={20} /></div>
                <div className="rounded-lg border border-line bg-panel2/50 p-4 text-center">
                  <div className="text-xs font-mono text-muted mb-1">STEP 3</div>
                  <div className="font-bold text-white">Order Execution</div>
                </div>
                <div className="flex justify-center text-muted/50"><ArrowRight className="rotate-90" size={20} /></div>
                <div className="rounded-lg border border-line bg-panel2/50 p-4 text-center">
                  <div className="text-xs font-mono text-muted mb-1">STEP 4</div>
                  <div className="font-bold text-white flex items-center justify-center gap-2"><Activity size={18} className="text-info"/> Fill Verification</div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* RESILIENCE & TELEGRAM */}
      <section className="py-24 bg-bg">
        <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
          <div className="grid lg:grid-cols-2 gap-12">
            <div className="rounded-2xl border border-line bg-bg p-8">
              <ServerCrash className="text-neg mb-6" size={32} />
              <h3 className="text-2xl font-bold text-white mb-4">Built to handle what can go wrong.</h3>
              <p className="text-muted mb-6">
                Use the Fault Simulation & Recovery Lab to test system resilience. The platform detects API timeouts, partial fills, rejected orders, and position mismatches, initiating automatic reconciliation.
              </p>
              <div className="space-y-3">
                <div className="flex items-center gap-3 text-sm text-text bg-panel2/50 p-3 rounded-lg"><span className="h-2 w-2 rounded-full bg-neg shrink-0"></span> Broker API timeout detected.</div>
                <div className="flex items-center gap-3 text-sm text-text bg-panel2/50 p-3 rounded-lg"><span className="h-2 w-2 rounded-full bg-warn shrink-0"></span> New unsafe orders blocked.</div>
                <div className="flex items-center gap-3 text-sm text-text bg-panel2/50 p-3 rounded-lg"><span className="h-2 w-2 rounded-full bg-info shrink-0"></span> Reconciliation compares internal and broker positions.</div>
                <div className="flex items-center gap-3 text-sm text-text bg-panel2/50 p-3 rounded-lg"><span className="h-2 w-2 rounded-full bg-pos shrink-0"></span> System state is synchronized.</div>
              </div>
            </div>
            
            <div className="rounded-2xl border border-line bg-bg p-8 flex flex-col">
              <Smartphone className="text-info mb-6" size={32} />
              <h3 className="text-2xl font-bold text-white mb-4">Your trading system. Within reach.</h3>
              <p className="text-muted mb-8">
                Authorized users can monitor and manage the platform remotely through Telegram. It uses the exact same risk controls and validation logic as the web dashboard.
              </p>
              <div className="flex-1 bg-panel border border-line rounded-xl p-4 font-mono text-xs overflow-hidden relative">
                <div className="absolute top-0 inset-x-0 h-6 bg-panel2/80 flex items-center px-4"><span className="text-muted font-sans font-semibold text-[10px] uppercase tracking-widest">Telegram Bot</span></div>
                <div className="mt-6 space-y-4">
                  <div className="flex justify-end"><div className="bg-info text-white p-2 rounded-lg rounded-tr-none inline-block">/status</div></div>
                  <div className="flex justify-start"><div className="bg-panel2 text-text p-2 rounded-lg rounded-tl-none inline-block border border-line">🟢 System is HEALTHY.<br/>3 strategies running.<br/>Risk utilization: 12%</div></div>
                  <div className="flex justify-end"><div className="bg-info text-white p-2 rounded-lg rounded-tr-none inline-block">/kill</div></div>
                  <div className="flex justify-start"><div className="bg-neg/20/50 border border-neg/30 text-red-300 p-2 rounded-lg rounded-tl-none inline-block">🚨 EMERGENCY KILL SWITCH INITIATED.<br/>All strategies paused.<br/>Closing 2 open positions.</div></div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* HOW IT WORKS */}
      <section id="how-it-works" className="py-32 bg-bg relative overflow-hidden">
        {/* Glow Effects */}
        <div className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-[800px] h-[300px] bg-info/5 blur-[120px] rounded-full pointer-events-none"></div>

        <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8 relative z-10">
          <div className="text-center mb-24">
            <h2 className="text-3xl md:text-4xl font-bold text-white mb-4">The Complete Trading Lifecycle</h2>
            <p className="text-muted max-w-2xl mx-auto">A streamlined, automated workflow from configuration to active risk management.</p>
          </div>
          
          <div className="relative">
            {/* Connecting Timeline Line (Desktop) */}
            <div className="hidden md:block absolute top-12 left-[10%] right-[10%] h-[2px] bg-gradient-to-r from-bg via-line to-bg">
              <div className="absolute top-0 left-0 h-full w-1/3 bg-gradient-to-r from-transparent via-info/50 to-transparent animate-[pulse_3s_ease-in-out_infinite]"></div>
            </div>

            <div className="grid md:grid-cols-4 gap-8">
              {[
                { num: '01', title: 'Connect', desc: 'Authenticate and connect an authorized trading account to the platform.', icon: Smartphone },
                { num: '02', title: 'Configure', desc: 'Choose strategies, adjust parameters, and define capital allocations and risk limits.', icon: ShieldCheck },
                { num: '03', title: 'Monitor', desc: 'Observe market candles, strategy signals, orders, positions, and P&L in the dashboard.', icon: Activity },
                { num: '04', title: 'Protect', desc: 'Let the Risk Firewall validate orders while recovery mechanisms handle abnormal conditions.', icon: ServerCrash },
              ].map((s, i) => (
                <div key={s.num} className="relative group">
                  {/* Timeline Circle Node */}
                  <div className="hidden md:flex absolute -top-4 left-1/2 -translate-x-1/2 w-8 h-8 rounded-full bg-panel border-2 border-line items-center justify-center group-hover:border-info group-hover:shadow-[0_0_15px_rgba(59,130,246,0.5)] transition-all duration-300 z-10">
                    <div className="w-2 h-2 rounded-full bg-line group-hover:bg-info transition-colors"></div>
                  </div>
                  
                  {/* Glassmorphic Card */}
                  <div className="mt-8 md:mt-12 p-8 rounded-2xl border border-line bg-panel hover:bg-panel2 hover:border-line transition-all duration-300 relative overflow-hidden group-hover:-translate-y-2 shadow-lg">
                    {/* Top gradient accent */}
                    <div className="absolute top-0 left-0 right-0 h-1 bg-gradient-to-r from-transparent via-info/0 to-transparent group-hover:via-info/50 transition-all duration-500"></div>
                    
                    <div className="flex items-center justify-between mb-6">
                      <div className="w-12 h-12 rounded-xl bg-bg border border-line flex items-center justify-center text-info group-hover:scale-110 transition-transform duration-300">
                        <s.icon size={22} />
                      </div>
                      <div className="text-4xl font-black text-line group-hover:text-line/80 transition-colors">{s.num}</div>
                    </div>
                    <h4 className="text-xl font-bold text-white mb-3">{s.title}</h4>
                    <p className="text-sm text-muted leading-relaxed">{s.desc}</p>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      </section>

      {/* CTA */}
      <section className="py-24 bg-panel border-y border-line relative overflow-hidden">
        <div className="absolute inset-0 bg-info/5"></div>
        <div className="mx-auto max-w-3xl px-4 sm:px-6 lg:px-8 text-center relative z-10">
          <h2 className="text-4xl font-bold text-white mb-6">Build a more controlled way to trade.</h2>
          <p className="text-lg text-muted mb-10">
            Explore your strategies, understand every execution, and keep risk management at the center of your trading workflow.
          </p>
          <div className="flex flex-col sm:flex-row justify-center items-center gap-4">
            <Link to="/login" className="w-full sm:w-auto rounded-md bg-info px-8 py-4 text-sm font-semibold text-white hover:bg-info transition-colors shadow-lg shadow-info/20">
              Open AlgoTrade
            </Link>
            <a href="#platform" className="w-full sm:w-auto rounded-md border border-line bg-panel2/50 px-8 py-4 text-sm font-semibold text-white hover:bg-panel2 border-line transition-colors">
              Explore Platform
            </a>
          </div>
        </div>
      </section>

      {/* FOOTER */}
      <footer className="bg-bg py-12 border-t border-line">
        <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
          <div className="grid md:grid-cols-4 gap-8 mb-12">
            <div className="md:col-span-2">
              <div className="flex items-center gap-2.5 mb-4">
                <img src="/logo.png" alt="AlgoTrade" className="h-7 w-7 rounded-full object-contain" />
                <span className="text-base font-bold tracking-tight text-white">AlgoTrade</span>
              </div>
              <p className="text-sm text-muted max-w-sm">
                A multi-strategy algorithmic trading platform with intelligent risk controls, execution visibility, and built-in resilience.
              </p>
            </div>
            <div>
              <h4 className="text-white font-semibold mb-4 text-sm">Platform</h4>
              <ul className="space-y-2 text-sm text-muted">
                <li><a href="#strategies" className="hover:text-text transition-colors">Strategies</a></li>
                <li><a href="#risk" className="hover:text-text transition-colors">Risk Management</a></li>
                <li><a href="#how-it-works" className="hover:text-text transition-colors">How It Works</a></li>
              </ul>
            </div>
            <div>
              <h4 className="text-white font-semibold mb-4 text-sm">Legal</h4>
              <ul className="space-y-2 text-sm text-muted">
                <li><a href="#" className="hover:text-text transition-colors">Privacy Policy</a></li>
                <li><a href="#" className="hover:text-text transition-colors">Terms of Service</a></li>
              </ul>
            </div>
          </div>
          <div className="border-t border-line pt-8 flex flex-col md:flex-row justify-between items-center gap-4 text-xs text-muted/50">
            <p>© {new Date().getFullYear()} AlgoTrade. All rights reserved.</p>
            <p className="max-w-2xl text-right">
              Disclaimer: Trading involves risk. Market data may be delayed or simulated depending on the configured provider. Strategy signals are not guarantees of future performance.
            </p>
          </div>
        </div>
      </footer>
    </div>
  )
}
