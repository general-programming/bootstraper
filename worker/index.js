import { Router } from 'itty-router'

// Create a new router
const router = Router()

const return_json = (payload, status_code) => {
    return new Response(JSON.stringify(payload), {
        status: status_code,
        headers: {
            'Content-Type': 'application/json',
        },
    })
}

/*
Our index route, a simple hello world.
*/
router.get('/', () => {
    return new Response('root')
})

/*
This route demonstrates path parameters, allowing you to extract fragments from the request
URL.

Try visit /example/hello and see the response.
*/
router.get('/example/:text', ({ params }) => {
    // Decode text like "Hello%20world" into "Hello world"
    let input = decodeURIComponent(params.text)

    // Return the HTML with the string to the client
    return new Response(`<p>hello: <code>${input}</code></p>`, {
        headers: {
            'Content-Type': 'text/html',
        },
    })
})

router.post('/assimilate/ipxe', async request => {
    // Reject responses that are not JSON.
    console.log(request.headers.get('Content-Type'))
    // if (request.headers.get('Content-Type') !== 'application/json') {
    //     return new Response(
    //         { status: 'invalid_type' },
    //         {
    //             headers: {
    //                 'Content-Type': 'application/json',
    //             },
    //         }
    //     )
    // }

    const formData = await request.formData()
    const body = {}
    for (const entry of formData.entries()) {
        body[entry[0]] = entry[1]
    }

    console.log(body)

    // Return a blank payload to the client.
    return new Response('#!ipxe\necho it works', {
        status: 200,
        headers: { 'Content-Type': 'text/plain' },
    })
})

router.post('/assimilate/json', async request => {
    // Reject responses that are not JSON.
    console.log(request.headers.get('Content-Type'))
    if (request.headers.get('Content-Type') !== 'application/json') {
        return return_json({ status: 'invalid_type' }, 400)
    }

    const payload = await request.json()
    console.log(payload)

    let result = {
        status: 'success',
    }
    return return_json(result, 200)
})

/*
This is the last route we define, it will match anything that hasn't hit a route we've defined
above, therefore it's useful as a 404 (and avoids us hitting worker exceptions, so make sure to include it!).

Visit any page that doesn't exist (e.g. /foobar) to see it in action.
*/
router.all('*', () => new Response('404', { status: 404 }))

/*
This snippet ties our worker to the router we deifned above, all incoming requests
are passed to the router where your routes are called and the response is sent.
*/
export default {
    fetch: router.handle,
}
