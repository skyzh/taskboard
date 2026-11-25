export function isBackendError(error) {
  return error?.status >= 500 && error.status < 600
}
