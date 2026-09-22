import { Button } from '@gouvfr-lasuite/ui-components';
import Head from 'next/head';
import { useRouter } from 'next/router';
import { ReactElement, useEffect } from 'react';
import { useTranslation } from 'react-i18next';

import Error401Svg from '@/assets/icons/error-401.svg';
import { Box, Text } from '@/components';
import { gotoLogin, useAuth } from '@/features/auth';
import { StandalonePageLayout } from '@/layouts';
import { NextPageWithLayout } from '@/types/next';

const SignInButton = () => {
  const { t } = useTranslation();

  return (
    <Button color="brand" size="small" onClick={() => gotoLogin(false)}>
      {t('Sign in')}
    </Button>
  );
};

const HeaderAuthActions = () => {
  const { t } = useTranslation();

  return (
    <Box $direction="row" $align="center" $gap="sm">
      <Button
        color="brand"
        variant="tertiary"
        size="small"
        onClick={() => gotoLogin(false)}
      >
        {t('Try it now')}
      </Button>
      <SignInButton />
    </Box>
  );
};

const Page: NextPageWithLayout = () => {
  const { t } = useTranslation();
  const { authenticated } = useAuth();
  const { replace } = useRouter();
  const pageTitle = `${t('401 Unauthorized')} - ${t('Docs')}`;

  useEffect(() => {
    if (authenticated) {
      void replace(`/`);
    }
  }, [authenticated, replace]);

  return (
    <>
      <Head>
        <meta name="robots" content="noindex" />
        <title>{pageTitle}</title>
        <meta property="og:title" content={pageTitle} key="title" />
      </Head>
      <Box
        $align="center"
        $gap="base"
        $padding={{ horizontal: 'base', bottom: 'lg' }}
        className="--docs--error-401"
      >
        <Box $align="center" $gap="xxxs">
          <Error401Svg aria-hidden="true" />
          <Text
            as="h1"
            $size="md"
            $weight="bold"
            $textAlign="center"
            $margin="0"
            $theme="neutral"
            $variation="primary"
          >
            {t('Access denied')}
          </Text>
          <Text
            as="p"
            $textAlign="center"
            $maxWidth="228px"
            $theme="neutral"
            $variation="secondary"
            $margin="0"
            $size="xs"
          >
            {t('Log in to access the document.')}
          </Text>
        </Box>
        <SignInButton />
      </Box>
    </>
  );
};

Page.getLayout = function getLayout(page: ReactElement) {
  return (
    <StandalonePageLayout headerActions={<HeaderAuthActions />}>
      {page}
    </StandalonePageLayout>
  );
};

export default Page;
