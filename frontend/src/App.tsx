import { CssBaseline, ThemeProvider } from '@mui/material';
import { Route, Routes } from 'react-router';
import { Layout } from './components/Layout';
import { DownloadPage } from './pages/DownloadPage';
import { LibraryBenefitPage } from './pages/LibraryBenefitPage';
import { LibraryPage } from './pages/LibraryPage';
import { LoginPage } from './pages/LoginPage';
import { NotFoundPage } from './pages/NotFoundPage';
import { ReceivePage } from './pages/ReceivePage';
import { SessionProvider } from './session';
import { theme } from './theme';

export default function App() {
  return (
    <ThemeProvider theme={theme}>
      <CssBaseline />
      <SessionProvider>
        <Layout>
          <Routes>
            <Route path="/r/:token" element={<ReceivePage />} />
            <Route path="/r/:token/download" element={<DownloadPage />} />
            <Route path="/login" element={<LoginPage />} />
            <Route path="/library" element={<LibraryPage />} />
            <Route path="/library/:benefitId" element={<LibraryBenefitPage />} />
            <Route path="*" element={<NotFoundPage />} />
          </Routes>
        </Layout>
      </SessionProvider>
    </ThemeProvider>
  );
}
