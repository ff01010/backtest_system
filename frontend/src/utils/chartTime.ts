export function toChartTimestamp(value: string): number {
  const match = value.match(
    /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})$/
  );
  if (!match) throw new Error(`无效图表时间: ${value}`);
  const [, year, month, day, hour, minute, second] = match;
  return Date.UTC(
    Number(year), Number(month) - 1, Number(day),
    Number(hour), Number(minute), Number(second)
  ) / 1000;
}


export function formatChartTimestamp(timestamp: number): string {
  const date = new Date(timestamp * 1000);
  const hour = String(date.getUTCHours()).padStart(2, '0');
  const minute = String(date.getUTCMinutes()).padStart(2, '0');
  return `${hour}:${minute}`;
}
