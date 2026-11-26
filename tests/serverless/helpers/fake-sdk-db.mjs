export const sql = (strings, ...values) => ({ text: strings.join('?'), values });
