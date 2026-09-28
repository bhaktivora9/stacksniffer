package com.acme.api;

import com.acme.repo.UserRepository;

public class UserController {
    private final UserRepository repository = new UserRepository();

    public String show(String id) {
        return repository.find(id);
    }
}
