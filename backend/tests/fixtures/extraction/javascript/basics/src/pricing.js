export const TAX = 0.2;

export function discount(cents) {
  return cents > 10000 ? round(cents * 0.9) : cents;
}

export function round(cents) {
  return Math.round(cents);
}
