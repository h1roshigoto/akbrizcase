type IconName =
  | 'box'
  | 'service'
  | 'search'
  | 'arrow'
  | 'check'
  | 'plus'
  | 'bag'
  | 'close'
const paths: Record<IconName, string> = {
  box: 'm12 3 9 5-9 5-9-5 9-5Zm-9 5v9l9 5 9-5V8M12 13v9M7.5 5.5l9 5',
  service: 'm14 6 4 4m-9 6-5 5-2-2 5-5m2-6a6 6 0 0 0 8 8l5-5-5 1-3-3 1-5-5 4Z',
  search: 'm21 21-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0Z',
  arrow: 'M4 12h16m-6-6 6 6-6 6',
  check: 'm5 12 4 4L19 6',
  plus: 'M12 5v14M5 12h14',
  bag: 'M5 7h14l1 14H4L5 7Zm3 0V5a4 4 0 0 1 8 0v2',
  close: 'm6 6 12 12M6 18 18 6',
}

export function Icon({
  name,
  className = '',
}: {
  name: IconName
  className?: string
}) {
  return (
    <svg
      className={className}
      width="20"
      height="20"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={paths[name]} />
    </svg>
  )
}
