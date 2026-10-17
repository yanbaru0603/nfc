import { createTheme } from '@mui/material/styles';

export const theme = createTheme({
  typography: {
    fontFamily: '"Hiragino Kaku Gothic ProN", "Yu Gothic", Meiryo, sans-serif',
    fontSize: 16,
    h1: { fontSize: '1.5rem', fontWeight: 700, lineHeight: 1.5 },
    h6: { fontSize: '1.125rem', fontWeight: 700 },
    body1: { fontSize: '1rem', lineHeight: 1.8 },
    body2: { fontSize: '0.875rem', lineHeight: 1.7 },
    button: { textTransform: 'none' },
  },
  palette: {
    primary: { main: '#245c73' },
    background: { default: '#f7f9fa' },
  },
  components: {
    MuiButton: { defaultProps: { size: 'large' } },
  },
});
