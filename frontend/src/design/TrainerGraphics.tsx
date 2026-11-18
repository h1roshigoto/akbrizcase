export type TrainerModel = '01' | '18'
export type TrainerSide = 'L' | 'C' | 'R'
export type Target = { height: number; x: number }
export type Detection = { zone: number; sides: TrainerSide[] }
export const centerTarget = { height: 1000, x: 500 }

export function detectionFor(model: TrainerModel, { height, x }: Target): Detection {
  const zone = Math.min(5, Math.floor(height * 6 / 2000))
  if (model === '18') {
    return { zone, sides: [x < 380 ? 'L' : x > 620 ? 'R' : 'C'] }
  }
  return { zone, sides: x < 440 ? ['L'] : x > 560 ? ['R'] : ['L', 'R'] }
}

export function formatHeight(height: number) {
  return (height / 1000).toFixed(2).replace('.', ',') + ' м'
}

const levels = [0, 1, 2, 3, 4, 5]
const yAt = (height: number) => 2300 - height
const halfBody: Array<[number, number]> = [
  [462,1528],[452,1500],[380,1478],[330,1452],[310,1400],[300,1250],[292,1080],[286,900],[292,830],
  [300,770],[322,745],[342,770],[340,840],[348,1060],[360,1230],[372,1330],[378,1150],[370,960],
  [380,860],[392,500],[402,120],[380,40],[380,0],[478,0],[478,60],[470,480],[482,830],[500,850],
]
const body = 'M' + halfBody.concat(halfBody.slice(0, -1).reverse().map(([x, h]): [number, number] => [1000 - x, h]))
  .map(([x, h]) => x + ' ' + yAt(h)).join('L') + 'Z'

export function TrainerArch({ target, detection, litZones, walking, sensors, metal, onTarget }: {
  target: Target
  detection: Detection | null
  litZones: number
  walking: boolean
  sensors: boolean
  metal: boolean
  onTarget: (target: Target) => void
}) {
  const guideY = yAt(target.height)
  return <figure className="tr-arch">
    <svg viewBox="0 0 1000 2400" role="img" aria-label="Учебный макет арки"
      onClick={event => {
        const matrix = event.currentTarget.getScreenCTM()
        if (!matrix) return
        const point = new DOMPoint(event.clientX, event.clientY).matrixTransform(matrix.inverse())
        if (point.x < 140 || point.x > 860 || point.y < 300 || point.y > 2300) return
        onTarget({
          height: Math.max(50, Math.min(1950, Math.round(yAt(point.y) / 10) * 10)),
          x: Math.max(180, Math.min(820, point.x)),
        })
      }}>
      <rect x="0" y="2300" width="1000" height="14" className="floor" />
      <rect x="140" y="300" width="720" height="2000" className="passage" />
      <g className="guide"><line x1="140" x2="860" y1={guideY} y2={guideY} /><text x="500" y={guideY - 30}>{formatHeight(target.height)}</text></g>
      <g className={walking ? 'person go' : 'person'}>
        <path d={body} className="body" /><ellipse cx="500" cy="655" rx="92" ry="112" className="body" />
      </g>
      <circle cx={target.x} cy={guideY} r="42" className={metal ? 'item show' : 'item'} />
      {[70, 860].map(x => <g key={x}>
        <rect x={x} y="135" width="70" height="2110" rx="4" className="post" />
        <rect x={x - 18} y="2245" width="106" height="55" rx="3" className="foot" />
        <path d={'M' + (x - 4) + ' 135 Q' + (x + 35) + ' 55 ' + (x + 74) + ' 135Z'} className="post" />
      </g>)}
      <rect x="140" y="150" width="720" height="220" rx="4" className="beam" />
      <rect x="330" y="180" width="340" height="150" rx="3" className="beam-p" />
      {(['L', 'R'] as const).map((side, column) => levels.map(zone => {
        const lit = zone < litZones || (detection?.zone === zone && (detection.sides.includes(side) || detection.sides.includes('C')))
        return <rect key={side + zone} x={98 + column * 790} y={1874 - zone * 290}
          width="14" height="262" rx="2" className={lit ? 'zled on' : 'zled'} />
      }))}
      {[146, 854].map(x => <circle key={x} cx={x} cy="1440" r="14" className={sensors ? 'sens on' : 'sens'} />)}
    </svg>
    <figcaption className="tip">Нажмите в проходе, чтобы выбрать, на какой высоте пронести предмет.</figcaption>
  </figure>
}

const digitSegments: Record<string, string> = {
  '0': 'abcdef', '1': 'bc', '2': 'abged', '3': 'abgcd', '4': 'fgbc', '5': 'afgcd',
  '6': 'afgedc', '7': 'abc', '8': 'abcdefg', '9': 'abcdfg', E: 'afged', H: 'fbgec',
  L: 'fed', d: 'bcdeg', r: 'eg', '-': 'g', O: 'abcdef', F: 'afeg', C: 'afed', A: 'abcefg',
}
const segmentRects = [
  ['a', 3, 0, 18, 4], ['g', 3, 21, 18, 4], ['d', 3, 42, 18, 4],
  ['f', 0, 3, 4, 19], ['b', 20, 3, 4, 19], ['e', 0, 24, 4, 19], ['c', 20, 24, 4, 19],
] as const

function Display({ y, value }: { y: number; value: string }) {
  const digits = value.slice(-4).padStart(4, ' ')
  return <g>{[0, 1, 2, 3].map(index => {
    const active = digitSegments[digits.charAt(index)] ?? ''
    return <g key={index} transform={'translate(' + (238 + index * 30) + ',' + y + ') skewX(-6)'}>
      {segmentRects.map(([key, x, top, width, height]) =>
        <rect key={key} x={x} y={top} width={width} height={height} rx="1.5" className={active.includes(key) ? 'seg on' : 'seg'} />)}
    </g>
  })}</g>
}

const sideLabel = { L: 'Л', C: 'Ц', R: 'П' }

export function TrainerPanel({ model, rows, ready, detection, litZones, testingIndicators }: {
  model: TrainerModel
  rows: [string, string]
  ready: boolean
  detection: Detection | null
  litZones: number
  testingIndicators: boolean
}) {
  const wide = model === '18'
  const sides: TrainerSide[] = wide ? ['L', 'C', 'R'] : ['L', 'R']
  return <svg className="tr-panel" viewBox="0 0 400 200" role="img" aria-label="Учебная панель индикации">
    <rect width="400" height="200" rx="4" className="p-bg" />
    <rect x="222" y="12" width="166" height="176" rx="2" className="p-in" />
    <circle cx="26" cy="24" r="8" className={ready || testingIndicators ? 'led g on' : 'led g'} />
    <text x="40" y="29" className="p-t">{wide ? 'ГОТОВ' : 'Готов'}</text>
    <circle cx="112" cy="24" r="8" className={detection || testingIndicators ? 'led r on' : 'led r'} />
    <text x="126" y="29" className="p-t">{wide ? 'ТРЕВОГА' : 'Тревога'}</text>
    {sides.map((side, column) => {
      const x = wide ? 18 + column * 50 : 22 + column * 92
      return <g key={side}>
        {levels.map(zone => <rect key={zone} x={x} y={168 - zone * 23} width={wide ? 42 : 66}
          height="13" rx="1" className={zone < litZones || (detection?.zone === zone && detection.sides.includes(side)) ? 'led r zone on' : 'led r zone'} />)}
        <text x={x + (wide ? 21 : 33)} y={wide ? 194 : 192} className="p-n">{sideLabel[side]}</text>
      </g>
    })}
    {levels.map(zone => <text key={zone} x={wide ? 176 : 196} y={179 - zone * 23} className="p-n">{zone + 1}</text>)}
    <Display y={wide ? 22 : 30} value={rows[0]} /><Display y={wide ? 96 : 112} value={rows[1]} />
    <text x="236" y={wide ? 84 : 98} className="p-t s">{wide ? 'ТРЕВОГА' : 'Проходы'}</text>
    <text x="236" y={wide ? 158 : 180} className="p-t s">{wide ? 'ПРОХОДЫ' : 'Тревоги'}</text>
    {wide && <>
      {Array.from({ length: 9 }, (_, level) => <rect key={level} x="192" y={176 - level * 15.5} width="18" height="10" rx="1"
        className={detection && level < 5 ? 'led r lvl on' : 'led r lvl'} />)}
      <text x="201" y="44" className="p-n">max</text>
      {['вкл', 'выбор', 'рег', 'ввод'].map((label, index) => <g key={label}>
        <rect x={236 + index * 38} y="164" width="30" height="11" rx="2" className="p-key" />
        <text x={251 + index * 38} y="186" className="p-n">{label}</text>
      </g>)}
    </>}
  </svg>
}
